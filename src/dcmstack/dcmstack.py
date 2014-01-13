"""
Stack DICOM datasets into volumes. The contents of this module are imported 
into the package namespace.
"""
import warnings, re, dicom
from copy import deepcopy
import itertools as it
import nibabel as nb
from nibabel.nifti1 import Nifti1Extensions
from nibabel.spatialimages import HeaderDataError
from nibabel.orientations import (io_orientation, 
                                  apply_orientation, 
                                  inv_ornt_aff,
                                  ornt_transform,
                                  axcodes2ornt
                                 )
import numpy as np
from .dcmmeta import DcmMeta, DcmMetaExtension, NiftiWrapper
import struct, itertools

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    from nibabel.nicom.dicomwrappers import (wrapper_from_data, 
                                             wrapper_from_file)

def make_key_regex_filter(exclude_res, force_include_res=None):
    '''Make a meta data filter using regular expressions.

    Parameters
    ----------
    exclude_res : sequence
        Sequence of regular expression strings. Any meta data where the key 
        matches one of these expressions will be excluded, unless it matches 
        one of the `force_include_res`.
    force_include_res : sequence
        Sequence of regular expression strings. Any meta data where the key 
        matches one of these expressions will be included.
        
    Returns
    -------
    A callable which can be passed to `DicomStack` as the `meta_filter`.
    '''
    exclude_re = re.compile('|'.join(['(?:' + regex + ')' 
                                      for regex in exclude_res])
                           )
    include_re = None
    if force_include_res:
        include_re = re.compile('|'.join(['(?:' + regex + ')' 
                                          for regex in force_include_res])
                               )

    def key_regex_filter(key, value):
        return (exclude_re.search(key) and 
                not (include_re and include_re.search(key)))
    return key_regex_filter

default_key_excl_res = ['Patient',
                        'Physician',
                        'Operator',
                        'Date', 
                        'Birth',
                        'Address',
                        'Institution',
                        'Station',
                        'SiteName',
                        'Age',
                        'Comment',
                        'Phone',
                        'Telephone',
                        'Insurance',
                        'Religious',
                        'Language',
                        'Military',
                        'MedicalRecord',
                        'Ethnic',
                        'Occupation',
                        'Unknown',
                        'Private',
                        'UID',
                        'StudyDescription',
                        'DeviceSerialNumber',
                        'ReferencedImageSequence',
                        'RequestedProcedureDescription',
                        'PerformedProcedureStepDescription',
                        'PerformedProcedureStepID',
                       ]
'''A list of regexes passed to `make_key_regex_filter` as `exclude_res` to 
create the `default_meta_filter`.'''
                        
default_key_incl_res = ['ImageOrientationPatient',
                        'ImagePositionPatient',
                       ]
'''A list of regexes passed to `make_key_regex_filter` as `force_include_res` 
to create the `default_meta_filter`.'''
                        
default_meta_filter = make_key_regex_filter(default_key_excl_res,
                                            default_key_incl_res)
'''Default meta_filter for `DicomStack`.'''

def reorder_voxels(vox_array, affine, voxel_order):
    '''Reorder the given voxel array and corresponding affine. 
    
    Parameters
    ----------
    vox_array : array
        The array of voxel data
    
    affine : array
        The affine for mapping voxel indices to Nifti patient space
    
    voxel_order : str
        A three character code specifing the desired ending point for rows, 
        columns, and slices in terms of the orthogonal axes of patient space: 
        (l)eft, (r)ight, (a)nterior, (p)osterior, (s)uperior, and (i)nferior.

    Returns
    -------
    out_vox : array
        An updated view of vox_array.
        
    out_aff : array
        A new array with the updated affine
        
    reorient_transform : array
        The transform used to update the affine.
        
    ornt_trans : tuple
        The orientation transform used to update the orientation.
        
    '''
    #Check if voxel_order is valid
    voxel_order = voxel_order.upper()
    if len(voxel_order) != 3:
        raise ValueError('The voxel_order must contain three characters')
    dcm_axes = ['LR', 'AP', 'SI']
    for char in voxel_order:
        if not char in 'LRAPSI':
            raise ValueError('The characters in voxel_order must be one '
                             'of: L,R,A,P,I,S')
        for idx, axis in enumerate(dcm_axes):
            if char in axis:
                del dcm_axes[idx]
    if len(dcm_axes) != 0:
        raise ValueError('No character in voxel_order corresponding to '
                         'axes: %s' % dcm_axes)
    
    #Check the vox_array and affine have correct shape/size
    if len(vox_array.shape) < 3:
        raise ValueError('The vox_array must be at least three dimensional')
    if affine.shape != (4, 4):
        raise ValueError('The affine must be 4x4')
    
    #Pull the current index directions from the affine
    orig_ornt = io_orientation(affine)
    new_ornt = axcodes2ornt(voxel_order)
    ornt_trans = ornt_transform(orig_ornt, new_ornt)
    orig_shape = vox_array.shape
    vox_array = apply_orientation(vox_array, ornt_trans)
    aff_trans = inv_ornt_aff(ornt_trans, orig_shape)
    affine = np.dot(affine, aff_trans)
    
    return (vox_array, affine, aff_trans, ornt_trans)

def dcm_time_to_sec(time_str):
    '''Convert a DICOM time value (value representation of 'TM') to the number 
    of seconds past midnight.
    
    Parameters
    ----------
    time_str : str
        The DICOM time value string
        
    Returns
    -------
    A floating point representing the number of seconds past midnight
    '''
    #Allow ACR/NEMA style format by removing any colon chars
    time_str = time_str.replace(':', '')
        
    #Only the hours portion is required
    result = int(time_str[:2]) * 3600

    str_len = len(time_str)
    if str_len > 2:
        result += int(time_str[2:4]) * 60
    if str_len > 4:
        result += float(time_str[4:])
    
    return float(result)
    
def get_n_vols(shape):
    '''Get the total number of two or three dimensional volumes for the given 
    shape.'''
    n_vols = 1
    for dim_size in shape[3:]:
        n_vols *= dim_size
    return n_vols

class IncongruentImageError(Exception):
    def __init__(self, msg):
        '''An exception denoting that a DICOM with incorrect size, pixel 
        spacing, or orientation was passed to `DicomStack.add_dcm`.'''
        self.msg = msg
        
    def __str__(self):
        return 'The image is not congruent to the existing stack: %s' % self.msg

class ImageCollisionError(Exception):
    '''An exception denoting that a DICOM which collides with one already in 
    the stack was passed to a `DicomStack.add_dcm`.'''
    def __str__(self):
        return 'The image collides with one already in the stack'
        
class InvalidStackError(Exception):
    def __init__(self, msg):
        '''An exception denoting that a `DicomStack` is not currently valid'''
        self.msg = msg
    
    def __str__(self):
        return 'The DICOM stack is not valid: %s' % self.msg

default_group_keys =  ('SeriesInstanceUID', 
                       'SeriesNumber', 
                       'ProtocolName',
                       'ImageOrientationPatient')
'''Default keys for grouping DICOM files that belong in the same 
multi-dimensional array together.'''

class DicomStack(object):
    '''Defines a method for stacking together DICOM data sets into a multi 
    dimensional volume. 
       
    Tailored towards creating NiftiImage output, but can also just create numpy 
    arrays. Can summarize all of the meta data from the input DICOM data sets 
    into a Nifti header extension (see `dcmmeta.DcmMeta`).

    Parameters
    ----------
    dim_orderings : list of str
        List of the DICOM keywords specifying how to order the DICOM data sets 
        along any extra-spatial dimensions. If not specified the ordering is 
        guessed based off the input data. If the input DICOM data has N 
        dimensions (N > 2), the first element in this list is used to order 
        dimension N + 1, the second N + 2, and so on.
    
    meta_filter : callable
        A callable that takes a meta data key and value, and returns True if 
        that meta data element should be excluded from the DcmMeta extension. 
        If not specified, defaults to `default_meta_filter`.
    '''
        
    sort_guesses = ['EchoTime',
                    'InversionTime',
                    'RepetitionTime',
                    'FlipAngle',
                    'TriggerTime',
                    'AcquisitionTime',
                    'ContentTime',
                    'AcquisitionNumber',
                    'InstanceNumber',
                   ]
    '''The meta data keywords used when trying to guess the sorting order. 
    Keys that come earlier in the list are given higher priority. Acquisition 
    parameters come first in the list, followed by time stamps, followed by 
    assigned numbers.'''
    
    minimal_keys = set(sort_guesses + 
                       ['Rows', 
                        'Columns', 
                        'PixelSpacing',
                        'ImageOrientationPatient',
                        'InPlanePhaseEncodingDirection',
                        'RepetitionTime',
                        'AcquisitionTime',
                       ] +
                       list(default_group_keys)
                      )
    '''Set of minimal meta data keys that should be provided if they exist in 
    the source DICOM files.'''
    
    def __init__(self, dim_orderings=None, meta_filter=None):
        self._dim_orderings = dim_orderings
        
        if meta_filter is None:
            self._meta_filter = default_meta_filter
        else:
            self._meta_filter = meta_filter
        
        #Sets all the state variables to their defaults
        self.clear()
        
    def _chk_equal(self, keys, meta1, meta2):
        for key in keys:
            if meta1[key] != meta2[key]:
                raise IncongruentImageError("%s does not match" % key)

    def _chk_congruent(self, meta):
        if not self._ref_input is None:
            self._chk_equal(('Rows',
                             'Columns',
                             'PixelSpacing', 
                             'ImageOrientationPatient'), 
                             meta, 
                             self._ref_input
                            )
    
    def add_dcm(self, dcm, meta=None):
        '''Add a pydicom dataset to the stack. 
        
        Parameters
        ----------
        dcm : dicom.dataset.Dataset
            The data set being added to the stack
            
        meta : dict
            The extracted meta data for the DICOM data set `dcm`. If None 
            extract.default_extractor will be used.
        
        Raises
        ------
        IncongruentImageError
            The provided `dcm` does not match the orientation or dimensions of 
            those already in the stack.
            
        ImageCollisionError
            The provided `dcm` has the same slice location and dim_orderings 
            values.

        '''                
        if meta is None:
            from .extract import default_extractor
            meta = default_extractor(dcm)

        #If this is the first dcm added, use it as the reference. Otherwise 
        #make sure it is congruent to existing reference
        if self._ref_input is None:
            self._ref_input = meta
        else:
            self._chk_congruent(meta)

        #Create a DicomWrapper for the input
        dw = wrapper_from_data(dcm)
        
        #Keep track of optional meta data that could be used when constructing 
        #the Nifti header
        self._phase_enc_dirs.add(meta.get('InPlanePhaseEncodingDirection'))
        self._repetition_times.add(meta.get('RepetitionTime'))
        
        #If dim_orderings was specified, pull out relevant values
        sorting_vals = []
        if not self._dim_orderings is None:
            n_ord = len(self._dim_orderings)
            for dim_idx in xrange(n_ord-1, -1, -1):
                dim_ord_key = self._dim_orderings[dim_idx]
                dim_ord_val = meta.get(dim_ord_key)
                sorting_vals.append(dim_ord_val)
                self._dim_order_vals[dim_idx].add(dim_ord_val)
            
        #Add the slice position at the end of sorting_vals
        slice_pos = dw.slice_indicator
        self._slice_pos_vals.add(slice_pos)
        sorting_vals.append(slice_pos)
        
        #Create a tuple with the sorting values
        sorting_tuple = tuple(sorting_vals)
        
        #If a explicit order was specified, raise an exception if image 
        #collides with another already in the stack
        if (not self._dim_orderings is None and 
            sorting_tuple in self._sorting_tuples
           ):
            raise ImageCollisionError()
        self._sorting_tuples.add(sorting_tuple)
        
        #Create a NiftiWrapper for this input and add it to our list
        nii_wrp = NiftiWrapper.from_dicom_wrapper(dw, meta)
        self._files_info.append((nii_wrp, sorting_tuple))
        
        #Set the dirty flags
        self._shape_dirty = True 
        self._meta_dirty = True
        
    def clear(self):
        '''Remove any DICOM datasets from the stack.'''
        if not self._dim_orderings is None:
            self._dim_order_vals = [set() 
                                    for dim in xrange(len(self._dim_orderings))
                                   ]
        self._slice_pos_vals = set()
        self._sorting_tuples = set()
        
        self._phase_enc_dirs = set()
        self._repetition_times = set()
        
        self._shape_dirty = True
        self._shape = None
    
        self._meta_dirty = True
        self._meta = None
        
        self._ref_input = None
        
        self._files_info = []
    
    def _sort_and_chk_order(self, slice_positions, files_per_vol, num_volumes, 
                   extra_dim_sizes, inferred_dim=None):
        #Sort the files
        self._files_info.sort(key=lambda x: x[1])
        if files_per_vol > 1:
            for vol_idx in range(num_volumes):
                start_slice = vol_idx * files_per_vol
                end_slice = start_slice + files_per_vol
                self._files_info[start_slice:end_slice] = \
                    sorted(self._files_info[start_slice:end_slice], 
                           key=lambda x: x[1][-1])
        
        #Figure out the number of files for each index in the extra dims
        files_per_idx = [files_per_vol]
        for dim_size in extra_dim_sizes[:-1]:
            files_per_idx.append(files_per_idx[-1] * dim_size)
        
        #For any extra dims whose size is not inferred or singular, sorting 
        #value should be constant for each index
        for dim, dim_size in enumerate(extra_dim_sizes):
            if inferred_dim is None or dim == inferred_dim or dim_size == 1:
                continue
            for dim_idx in xrange(dim_size):
                start_idx = dim_idx * files_per_idx[dim]
                end_idx = start_idx + files_per_idx[dim]
                sort_val = self._files_info[start_idx][1][-1 - dim]
                for file_info in self._files_info[start_idx:end_idx]:
                    if file_info[1][-1 - dim] != sort_val:
                        raise InvalidStackError("Sorting values not constant "
                                                "over each index")
                
        #Check that slice positions are consistent across all volumes
        if files_per_vol > 1:
            for vol_idx in xrange(num_volumes):
                start_idx = vol_idx * files_per_vol
                for slice_idx, slice_pos in enumerate(slice_positions):
                    file_info = self._files_info[start_idx+slice_idx]
                    if file_info[1][-1] != slice_pos:
                        raise InvalidStackError("Missing or duplicate slice.")
        
    def get_shape(self):
        '''Get the shape of the stack.
        
        Returns
        -------
        A tuple of integers giving the size of the dimensions of the stack.
        
        Raises
        ------
        InvalidStackError
            The stack is incomplete or invalid.
        '''
        #If the dirty flag is not set, return the cached value
        if not self._shape_dirty:
            return self._shape

        #We need at least one file in the stack
        if len(self._files_info) == 0:
            raise InvalidStackError("No files in the stack")
        
        #Figure out number of files per volume
        files_per_vol = len(self._slice_pos_vals)
        
        #Get the slice positions is sorted order
        slice_positions = sorted(list(self._slice_pos_vals))
        
        #If more than one file per volume, check that slice spacing is equal
        if files_per_vol > 1:
            spacings = []
            for idx in xrange(files_per_vol - 1):
                spacings.append(slice_positions[idx+1] - slice_positions[idx])
            spacings = np.array(spacings)
            avg_spacing = np.mean(spacings)
            if not np.allclose(avg_spacing, spacings, rtol=4e-2):
                raise InvalidStackError("Slice spacings are not consistent")
        
        #Simple check for an incomplete stack
        if len(self._files_info) % files_per_vol != 0:
            raise InvalidStackError("Number of files is not an even multiple "
                                    "of the number of unique slice positions.")
        
        #The number of individual (hyper) volumes (3 or more dimensions)
        num_volumes = len(self._files_info) / files_per_vol
        
        #If we are joining files along an extra-spatial dim and no ordering is 
        #specified, we try to guess how to order along that dim
        if num_volumes > 1 and self._dim_orderings is None:
            extra_dim_sizes = [num_volumes]
            
            #Get a list of possible sort orders
            possible_orders = []
            for key in self.sort_guesses:
                vals = set([file_info[0].get_meta(key)
                            for file_info in self._files_info]
                          )
                #Need at least as many values as volumes for a unique ordering
                if len(vals) >= num_volumes:
                    possible_orders.append(key)
                    
            #Try out each possible sort order
            for ordering in possible_orders:
                #Update sorting tuples, make sure they are unique
                self._sorting_tuples = set()
                for idx in xrange(len(self._files_info)):
                    nii_wrp, curr_tuple = self._files_info[idx] 
                    new_tuple = (nii_wrp[ordering], curr_tuple[0])
                    if new_tuple in self._sorting_tuples:
                        continue
                    self._sorting_tuples.add(new_tuple)
                    self._files_info[idx] = (nii_wrp, new_tuple)
                                               
                #Sort and check the order
                try:
                    self._sort_and_chk_order(slice_positions,
                                             files_per_vol, 
                                             num_volumes, 
                                             [num_volumes],
                                             0
                                            )
                except InvalidStackError:
                    pass
                else:
                    break
            else:
                raise InvalidStackError("Unable to guess key for sorting "
                                        "last dimension")
        else:
            #If there are extra-spatital dimensions and the order is defined 
            #we must determine their size
            if not self._dim_orderings is None and num_volumes > 1:
                #We can infer the size of the first additional extra-spatial
                #dimension from the number of (hyper) volumes plus the size of 
                #any other extra dimensions. If there are other extra 
                #dimensions, their size must be equal to the number of unique 
                #values for their sorting key
                extra_dim_sizes = [len(vals) for vals in self._dim_order_vals]
                n_non_single = 0
                first_non_single = None
                for dim_idx, dim_size in enumerate(extra_dim_sizes):
                    if dim_size != 1:
                        if first_non_single is None:
                            first_non_single = dim_idx
                        n_non_single += 1
                if n_non_single == 1:
                    extra_dim_sizes[first_non_single] = num_volumes
                    inferred_dim = first_non_single
                else:
                    #If the first dim has as many or more values than volumes 
                    #it must be inferred
                    if extra_dim_sizes[first_non_single] >= num_volumes:
                        extra_dim_sizes[first_non_single] = num_volumes
                        inferred_dim = first_non_single
                        for dim_size in extra_dim_sizes[first_non_single + 1:]:
                            extra_dim_sizes[first_non_single] /= dim_size
                
            else:
                extra_dim_sizes = []
                inferred_dim = None
                
            #Sort the files and check if the ordering is valid
            self._sort_and_chk_order(slice_positions,
                                     files_per_vol, 
                                     num_volumes, 
                                     extra_dim_sizes,
                                     inferred_dim
                                    )
        
        #Stack appears to be valid, build the shape tuple
        file_shape = self._files_info[0][0].nii_img.get_shape()
        vol_shape = list(file_shape)
        if files_per_vol > 1:
            vol_shape[2] = files_per_vol 
        shape = vol_shape + extra_dim_sizes
        
        #Strip trailing singular dimensions
        for dim_idx in xrange(len(shape)-1, 2, -1):
            if shape[dim_idx] == 1:
                shape = shape[:-1]
            else:
                break
        
        #Cache the result
        self._shape = tuple(shape)          
        self._shape_dirty = False
        return self._shape

    def get_data(self):
        '''Get an array of the voxel values.
        
        Returns
        -------
        A numpy array filled with values from the DICOM data sets' pixels.
        
        Raises
        ------
        InvalidStackError
            The stack is incomplete or invalid.
        '''
        #Create an array for storing the voxel data
        stack_shape = self.get_shape()
        stack_dtype = self._files_info[0][0].nii_img.get_data_dtype()
        #This is a hack to keep fslview happy, Shouldn't cause issues as the 
        #original data should be 12-bit and any scaling will result in float 
        #data
        if stack_dtype == np.uint16:
            stack_dtype = np.int16
        vox_array = np.empty(stack_shape, dtype=stack_dtype)        
        
        #Figure out files per volume, file shape/n_dims, etc
        n_vols = get_n_vols(stack_shape)
        files_per_vol = len(self._files_info) / n_vols
        file_shape = self._files_info[0][0].nii_img.get_shape()
        file_n_dims = len(file_shape)
        
        #If the file data is 2D we need to account for singular last dimension
        src_idx = [slice(None) for _ in xrange(file_n_dims)]
        if file_n_dims == 3 and file_shape[2] == 1:
            file_n_dims = 2
            src_idx[2] = 0
            
        #Number of files for each of the indices we are iterating over
        files_per_idx = [1]
        for dim_size in stack_shape[file_n_dims:]:
            files_per_idx.append(files_per_idx[-1] * dim_size)
            
        #We iterate over any dimensions higher than the file's dimensions, 
        #copying a file's data on each iteration
        idx_iters = ([[slice(None)]
                      for _ in xrange(file_n_dims)
                     ] + 
                     [xrange(dim_size) 
                      for dim_size in stack_shape[file_n_dims:]
                     ]
                    )
        for dest_idx in it.product(*idx_iters):
            file_idx = sum(dest_idx[file_n_dims + dim] * files_per_idx[dim] 
                           for dim in xrange(len(stack_shape) - file_n_dims))
            nii_img = self._files_info[file_idx][0].nii_img
            vox_array[dest_idx] = nii_img.get_data()[src_idx]
        
        return vox_array
        
    def get_affine(self):
        '''Get the affine transform for mapping row/column/slice indices 
        to Nifti (RAS) patient space.
        
        Returns
        -------
        A 4x4 numpy array containing the affine transform.
        
        Raises
        ------
        InvalidStackError
            The stack is incomplete or invalid.
        '''
        #Figure out the number of three (or two) dimensional volumes
        shape = self.get_shape()
        n_vols = get_n_vols(shape)
        
        #Figure out the number of files in each volume
        files_per_vol = len(self._files_info) / n_vols
        
        #Pull the DICOM Patient Space affine from the first input
        aff = self._files_info[0][0].nii_img.get_affine()
        
        #If there is more than one file per volume, we need to fix slice scaling
        if files_per_vol > 1:
            first_offset = aff[:3, 3]
            second_offset = self._files_info[1][0].nii_img.get_affine()[:3, 3]
            scaled_slc_dir = second_offset - first_offset
            aff[:3, 2] = scaled_slc_dir
        
        return aff
        
    def to_nifti(self, voxel_order='LAS', embed_meta=False):
        '''Returns a NiftiImage with the data and affine from the stack.
        
        Parameters
        ----------
        voxel_order : str
            A three character string repsenting the voxel order in patient 
            space (see the function `reorder_voxels`). Can be None or an empty 
            string to disable reorientation.
            
        embed_meta : bool
            If true a dcmmeta.DcmMetaExtension will be embedded in the Nifti 
            header.
            
        Returns
        -------
        A nibabel.nifti1.Nifti1Image created with the stack's data and affine. 
        '''
        #Get the voxel data and affine   
        data = self.get_data()
        affine = self.get_affine()
        
        #Figure out the number of 2D/3D volumes and files per volume
        n_vols = get_n_vols(data.shape)
        files_per_vol = len(self._files_info) / n_vols 
        
        #Reorder the voxel data if requested
        permutation = [0, 1, 2]
        slice_dim = 2
        reorient_transform = np.eye(4)
        if voxel_order:
            (data, 
             affine,
             reorient_transform,
             ornt_trans) = reorder_voxels(data, affine, voxel_order)
            permutation, flips = zip(*ornt_trans)
            permutation = [int(val) for val in permutation]
            
            #Reverse file order in each volume's files if we flipped slice order
            #This will keep the slice times and meta data order correct
            if files_per_vol > 1 and flips[slice_dim] == -1:
                self._shape_dirty = True
                for vol_idx in xrange(n_vols):
                    start = vol_idx * files_per_vol
                    stop = start + files_per_vol
                    self._files_info[start:stop] = [self._files_info[idx] 
                                                    for idx in xrange(stop - 1, 
                                                                      start - 1, 
                                                                      -1)
                                                   ]
            
            #Update the slice dim
            slice_dim = permutation[2]
        
        #Create the nifti image using the data array
        nifti_image = nb.Nifti1Image(data, affine)
        nifti_header = nifti_image.get_header()
        
        #Set the units and dimension info
        nifti_header.set_xyzt_units('mm', 'msec')
        if len(self._repetition_times) == 1 and not None in self._repetition_times:
            nifti_header['pixdim'][4] = list(self._repetition_times)[0]
        dim_info = {'freq' : None, 'phase' : None, 'slice' : slice_dim}
        if len(self._phase_enc_dirs) == 1 and not None in self._phase_enc_dirs:
            phase_dir = list(self._phase_enc_dirs)[0]
            if phase_dir == 'ROW':
                dim_info['phase'] = permutation[1]
                dim_info['freq'] = permutation[0]
            else:
                dim_info['phase'] = permutation[0]
                dim_info['freq'] = permutation[1]
        nifti_header.set_dim_info(**dim_info)
        n_slices = data.shape[slice_dim]
        
        #Set the slice timing header info
        has_acq_time = (self._files_info[0][0].get_meta('AcquisitionTime') != 
                        None)
        if files_per_vol > 1 and has_acq_time:
            #Pull out the relative slice times for the first volume
            slice_times = np.array([dcm_time_to_sec(file_info[0]['AcquisitionTime'])
                                    for file_info in self._files_info[:n_slices]]
                                  )
            slice_times -= np.min(slice_times)
            
            #If there is more than one volume, check if times are consistent
            is_consistent = True
            for vol_idx in xrange(1, n_vols):
                start_slice = vol_idx * n_slices
                end_slice = start_slice + n_slices
                slices_info = self._files_info[start_slice:end_slice]
                vol_slc_times = \
                    np.array([dcm_time_to_sec(file_info[0]['AcquisitionTime'])
                              for file_info in slices_info]
                            )
                vol_slc_times -= np.min(vol_slc_times)
                if not np.allclose(slice_times, vol_slc_times):
                    is_consistent = False
                    break
                
            #If the times are consistent and not all zero, try setting the slice 
            #times (sets the slice duration and code if possible).
            if is_consistent and not np.allclose(slice_times, 0.0):
                try:
                    nifti_header.set_slice_times(slice_times)
                except HeaderDataError:
                    pass
                
        #Embed the meta data extension if requested
        if embed_meta:
            #Iteratively merge meta along each dimension from smallest to 
            #largest
            if files_per_vol > 1:
                start_dim = 2
            else:
                start_dim = len(self._files_info[0][0].nii_img.get_shape())
            end_dim = len(data.shape)
            exts = [file_info[0].meta_ext for file_info in self._files_info]
            for merge_dim in xrange(start_dim, end_dim):
                if merge_dim < 3 :
                    seq_size = data.shape[slice_dim]
                else:
                    seq_size = data.shape[merge_dim]
                n_seqs = len(exts) / seq_size
                merged = []
                for seq_idx in xrange(n_seqs):
                    start = seq_idx * seq_size
                    end = start + seq_size
                    merged.append(DcmMeta.from_sequence(exts[start:end], 
                                                        merge_dim)
                                 )
                exts = merged
            meta_ext = exts[0] 
            
            meta_ext.shape = data.shape
            meta_ext.slice_dim = slice_dim
            meta_ext.affine = nifti_header.get_best_affine()
            meta_ext.reorient_transform = reorient_transform
                    
            #Filter and embed the meta data
            meta_ext.filter_meta(self._meta_filter)
            nifti_ext = DcmMetaExtension.from_dcmmeta(meta_ext)
            nifti_header.extensions = Nifti1Extensions([nifti_ext])

        nifti_image.update_header()
        return nifti_image
        
    def to_nifti_wrapper(self, voxel_order=''):
        '''Convienance method. Calls `to_nifti` and returns a `NiftiWrapper` 
        generated from the result.
        '''
        return NiftiWrapper(self.to_nifti(voxel_order, True))


class DicomStackOnline(DicomStack):

    def _init_dataset(self):
        if hasattr(self,'_is_init') and self._is_init:
            return
        # try to find information about dataset in a single dicom
        self._shape = None        
        self._slice_order = None
        self.frame_idx, self.slice_idx = 0, 0

        dicom_source, self._dicom_source = itertools.tee(self._dicom_source)
        dw = wrapper_from_data(dicom_source.next())
        self._shape = dw.image_shape
        self._nframes_per_dicom = 1
        self.nframes, self.nslices = 0, 0
        if len(self._shape) < 3:
            self._nslices_per_dicom = 1
            self._nframes_per_dicom = 0
            if not dw.get((0x2001, 0x1018)) is None:
                self.nslices = dw.get((0x2001,0x1018)).value
            elif not dw.get((0x0021,0x104f)) is None:
                self.nslices = dw.get((0x0021,0x104f)).value
            if isinstance(self.nslices, str):
                self.nslices = struct.unpack('i', self.nslices)[0]
                self.nslices = int(self.nslices)
            self._shape += (self.nslices,)
        else:
            self._nslices_per_dicom = self._shape[2]
            self.nslices = self._shape[2]
        if len(self._shape) < 4:
            if not dw.get('NumberOfTemporalPositions') is None:
                self.nframes = int(dw.get('NumberOfTemporalPositions'))
            self._shape += (self.nframes,)
        else:
            self._nframes_per_dicom = self._shape[3]
            self.nframes = self._shape[3]
            
        if dw.is_mosaic:
            self._slice_trigger_times = dw.csa_header['tags'].get(
                'MosaicRefAcqTimes')['items']
            self._slice_order = np.argsort(self._slice_trigger_times)
        elif dw.is_multiframe:
            self._slice_order = np.arange(self.nslices)
            tr=dw.shared.MRTimingAndRelatedParametersSequence[0].RepetitionTime
            self._slice_trigger_times = np.linspace(
                0, tr*1e-3, self.nslices+1)[:-1]
        else:
            self._slice_locations = []
            self._slice_trigger_times = []
            while not dw.slice_indicator in self._slice_locations:
                self._slice_locations.append(dw.slice_indicator)
                tt = dw.get((0x0018, 0x1060)) #TriggerTime
                if tt is None:
                    tt = dw.get((0x0021, 0x105e)) #RTIA Timer
                if not tt  is None:
                    self._slice_trigger_times.append(float(tt.value))
                df = dicom_source.next()
                dw = wrapper_from_data(df)
            if self.nslices ==0:
                self.nslices = len(self._slice_locations)
            if not len(self._slice_trigger_times):
                self._slice_trigger_times = np.linspace(
                    0, dw.get('RepetitionTime')*1e-3, self.nslices+1)[:-1]
            else:
                self._slice_trigger_times = [self._slice_trigger_times[i] \
                    for i in np.argsort(self._slice_locations)]
            self._slice_order = np.argsort(self._slice_trigger_times)
            self._slice_locations = sorted(self._slice_locations)
        self._is_init = True


    def set_source(self, dicom_source):
        self._dicom_source = dicom_source

    def iter_frame(self, data=True):
        # iterate on each acquired volume
        self._init_dataset()
        frame_data = None
        for df in self._dicom_source:
            dw = wrapper_from_data(df)
            nw = NiftiWrapper.from_dicom_wrapper(dw)
            if self._nframes_per_dicom is 1:
                if data:
                    frame_data = nw.nii_img.get_data()
                yield self.frame_idx, nw.nii_img.get_affine(), frame_data
                self.frame_idx += 1
            elif self._nframes_per_dicom > 1:
                if data:
                    frames_data = nw.nii_img.get_data()
                for t in xrange(self._shape[-1]):
                    if data:
                        frame_data = frames_data[...,t]
                    yield nframe, nw.nii_img.get_affine(), frame_data
                    self.frame_idx += 1
            else:
                if data:
                    pos = self._slice_locations.index(dw.slice_indicator)
                    if frame_data is None:
                        frame_data = np.empty(self._shape[:3])
                    frame_data[...,pos] = np.squeeze(nw.nii_img.get_data())
                self.slice_idx += 1
                if self.slice_idx == self.nslices:
                    self.frame_idx += 1
                    self.slice_idx = 0
                    yield self.frame_idx-1, nw.nii_img.get_affine(), frame_data
    
    def iter_slices(self, data=True, slice_order='acq_time'):
        # iterate on each slice in the temporal order they are acquired
        self._init_dataset()
        slices_buffer = [None]*self.nslices

        slice_seq = self._slice_order
        if slice_order is 'ascending':
            slice_seq = np.arange(0,self.nslices)
        elif slice_order is 'descending':
            slice_seq = np.arange(self.nslices,0,-1)-1

        for df in self._dicom_source:
            dw = wrapper_from_data(df)
            nw = NiftiWrapper.from_dicom_wrapper(dw)
            slice_data = None
            if self._nframes_per_dicom is 1:
                if data:
                    frame_data = nw.nii_img.get_data()
                for sl in slice_seq:
                    if data:
                        slice_data = frame_data[...,sl]
                    yield self.frame_idx, sl, nw.nii_img.get_affine(), \
                        self._slice_trigger_times[sl], slice_data
                self.frame_idx += 1
            elif self._nframes_per_dicom > 1:
                if data:
                    frames_data = nw.nii_img.get_data()
                for t in xrange(self._nframes_per_dicom):
                    for sl in slice_seq:
                        if data:
                            slice_data = frames_data[...,sl,t]
                            yield self.frame_idx, sl, nw.nii_img.get_affine(),\
                                self._slice_trigger_times[sl], slice_data
                    self.frame_idx += 1
            else:
                # buffer incoming slices to
                pos = self._slice_locations.index(dw.slice_indicator)
                slices_buffer[pos] = dw,nw
                sl = slice_seq[self.slice_idx]
                while slices_buffer[sl] is not None:
                    dw,nw = slices_buffer[sl]
                    slices_buffer[sl] = None
                    if data:
                        slice_data = nw.nii_img.get_data()[...,0]
                    yield self.frame_idx, sl, nw.nii_img.get_affine(), \
                        self._slice_trigger_times[sl], slice_data
                    self.slice_idx += 1
                    if self.slice_idx == self.nslices:
                        self.frame_idx += 1
                        self.slice_idx = 0
                    sl = slice_seq[self.slice_idx]
            del dw
        
def parse_and_group(src_paths, group_by=default_group_keys, extractor=None, 
                    force=False, warn_on_except=False, 
                    defer_size=None,stop_before_pixels=False):
    '''Parse the given dicom files and group them together. Each group is 
    stored as a (list) value in a dict where the key is a tuple of values 
    corresponding to the keys in 'group_by'
    
    Parameters
    ----------
    src_paths : sequence
        A list of paths to the source DICOM files.
        
    group_by : tuple
        Meta data keys to group data sets with. Any data set with the same 
        values for these keys will be grouped together. This tuple of values 
        will also be the key in the result dictionary.
        
    extractor : callable
        Should take a dicom.dataset.Dataset and return a dictionary of the 
        extracted meta data. 
        
    force : bool
        Force reading source files even if they do not appear to be DICOM.
        
    warn_on_except : bool
        Convert exceptions into warnings, possibly allowing some results to be 
        returned.
        
    Returns
    -------
    groups : dict
        A dict mapping tuples of values (corresponding to 'group_by') to groups 
        of data sets. Each element in the list is a tuple containing the dicom 
        object, the parsed meta data, and the filename.
    '''
    if extractor is None:
        from .extract import default_extractor
        extractor = default_extractor
        
    results = {}
    for dcm_path in src_paths:
        #Read the DICOM file
        try:
            dcm = dicom.read_file(dcm_path, force=force, 
                                  defer_size=defer_size,
                                  stop_before_pixels=stop_before_pixels)
        except Exception, e:
            if warn_on_except:
                warnings.warn('Error reading file %s: %s' % (dcm_path, str(e)))
                continue
            else:
                raise
            
        #Extract the meta data and group 
        meta = extractor(dcm)
        key_list = []
        for grp_key in group_by:
            key_elem = meta.get(grp_key)
            if isinstance(key_elem, list):
                key_elem = tuple(key_elem)
            key_list.append(key_elem)
        key = tuple(key_list)
        if not key in results:
            results[key] = []
            
        results[key].append((dcm, meta, dcm_path))
        
    return results
    
def stack_group(group, warn_on_except=False, **stack_args):
    result = DicomStack(**stack_args)
    for dcm, meta, fn in group:
        try:
            result.add_dcm(dcm, meta)
        except Exception, e:
            if warn_on_except:
                warnings.warn('Error adding file %s to stack: %s' % 
                              (fn, str(e)))
            else:
                raise
    return result
    
def parse_and_stack(src_paths, group_by=default_group_keys, extractor=None, 
                    force=False, warn_on_except=False, **stack_args):
    '''Parse the given dicom files into a dictionary containing one or more 
    DicomStack objects.
    
    Parameters
    ----------
    src_paths : sequence
        A list of paths to the source DICOM files.
        
    group_by : tuple
        Meta data keys to group data sets with. Any data set with the same 
        values for these keys will be grouped together. This tuple of values 
        will also be the key in the result dictionary.
        
    extractor : callable
        Should take a dicom.dataset.Dataset and return a dictionary of the 
        extracted meta data. 
        
    force : bool
        Force reading source files even if they do not appear to be DICOM.
        
    warn_on_except : bool
        Convert exceptions into warnings, possibly allowing some results to be 
        returned.
    
    stack_args : kwargs
        Keyword arguments to pass to the DicomStack constructor.
    '''
    results = parse_and_group(src_paths, 
                              group_by, 
                              extractor, 
                              force, 
                              warn_on_except)
                              
    for key, group in results.iteritems():
        results[key] = stack_group(group, warn_on_except, **stack_args)
    
    return results
