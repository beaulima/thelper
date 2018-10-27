"""Transformations module.

This module contains image transformation classes and wrappers for
preprocessing, data augmentation, and normalization.

All transforms should aim to be compatible with both numpy arrays and
PyTorch tensors. By default, images are processed using ``__call__``,
meaning that for a given transformation ``t``, we apply it via::

    image_transformed = t(image)

All important parameters for an operation should also be passed in the
constructor and exposed in the operation's ``__repr__`` function so that
external parsers can discover exactly how to reproduce their behavior.
"""
import copy
import itertools
import logging
import math

import Augmentor
import cv2 as cv
import numpy as np
import PIL.Image
import torch
import torchvision.transforms
import torchvision.utils

import thelper.utils

logger = logging.getLogger(__name__)


def load_transforms(stages):
    """Loads a transformation pipeline from a list of stages.

    Each entry in the provided list will be considered a stage in the pipeline. The ordering of the stages
    is important, as some transformations might not be compatible if taken out of order. The entries must
    each be dictionaries that define an operation, its parameters, and some meta-parameters (detailed below).

    The ``operation`` field of each stage will be used to dynamically import a specific type of operation to
    apply. The ``parameters`` field of each stage will then be used to pass parameters to the constructor of
    this operation. If an operation is identified as ``"Augmentor.Pipeline"``, it will be specially handled
    as an Augmentor pipeline, and its parameters will be parsed further (a dictionary is expected). Three
    fields are required in this dictionary: ``input_tensor`` (bool) which specifies whether the previous stage
    provides a ``torch.Tensor`` to the augmentor pipeline; ``output_tensor`` (bool) which specifies whether the
    output of the augmentor pipeline should be converted into a ``torch.Tensor``; and ``operations`` (dict)
    which specifies the Augmentor pipeline operation names and parameters (as a dictionary).

    Usage examples inside a session configuration file::
        # ...
        # the 'data_config' field can contain several transformation pipelines
        # (see 'thelper.data.load' for more information on these pipelines)
        "data_config": {
            # ...
            # the 'base_transforms' operations are applied to all loaded samples
            "base_transforms": [
                {
                    "operation": "...",
                    "parameters": {
                        ...
                    }
                },
                {
                    "operation": "...",
                    "parameters": {
                        ...
                    }
                }
            ],
        # ...

    Args:
        stages: a list defining a series of transformations to apply as a single pipeline.

    Returns:
        A transformation pipeline object compatible with the ``torchvision.transforms`` interfaces.

    .. seealso::
        :class:`thelper.transforms.AugmentorWrapper`
        :func:`thelper.transforms.load_augments`
        :func:`thelper.data.load`
    """
    if not isinstance(stages, list):
        raise AssertionError("expected stages to be provided as a list")
    logger.debug("loading transforms stages...")
    if not stages:
        return None, True  # no-op transform, and dont-care append
    if not isinstance(stages[0], dict):
        raise AssertionError("expected each stage to be provided as a dictionary")
    operations = []
    for stage_idx, stage in enumerate(stages):
        if "operation" not in stage or not stage["operation"]:
            raise AssertionError("stage #%d is missing its operation field" % stage_idx)
        operation_name = stage["operation"]
        if "parameters" in stage and not isinstance(stage["parameters"], dict):
            raise AssertionError("stage #%d parameters are not provided as a dictionary" % stage_idx)
        operation_params = stage["parameters"] if "parameters" in stage else {}
        if operation_name == "Augmentor.Pipeline":
            augp = Augmentor.Pipeline()
            if "input_tensor" not in operation_params:
                raise AssertionError("missing mandatory augmentor pipeline config 'input_tensor' field")
            if "output_tensor" not in operation_params:
                raise AssertionError("missing mandatory augmentor pipeline config 'output_tensor' field")
            if "operations" not in operation_params:
                raise AssertionError("missing mandatory augmentor pipeline config 'operations' field")
            augp_operations = operation_params["operations"]
            if not isinstance(augp_operations, dict):
                raise AssertionError("augmentor pipeline 'operations' field should contain dictionary")
            for augp_op_name, augp_op_params in augp_operations.items():
                getattr(augp, augp_op_name)(**augp_op_params)
            if operation_params["input_tensor"]:
                operations.append(torchvision.transforms.ToPILImage())
            operations.append(AugmentorWrapper(augp))
            if operation_params["output_tensor"]:
                operations.append(torchvision.transforms.ToTensor())
        else:
            operation_type = thelper.utils.import_class(operation_name)
            operation = operation_type(**operation_params)
            operations.append(operation)
    if len(operations) > 1:
        return thelper.transforms.Compose(operations)
    elif len(operations) == 1:
        return operations[0]
    else:
        return None


def load_augments(config):
    """Loads a data augmentation pipeline.

    An augmentation pipeline is essentially a specialized transformation pipeline that can be appended or
    prefixed to the base transforms defined for all samples. Most importantly, it can increase the number
    of samples in a dataset based on the duplication/tiling of input samples from the dataset parser.

    Usage examples inside a session configuration file::
        # ...
        # the 'data_config' field can contain several augmentation pipelines
        # (see 'thelper.data.load' for more information on these pipelines)
        "data_config": {
            # ...
            # the 'train_augments' operations are applied to training samples only
            "train_augments": {
                # specifies whether to apply the augmentations before or after the base transforms
                "append": false,
                "transforms": [
                    {
                        # here, we use a single stage, which is actually an augmentor sub-pipeline
                        # that is purely probabilistic (i.e. it does not increase input sample count)
                        "operation": "Augmentor.Pipeline",
                        "parameters": {
                            # we assume input images are still provided in numpy/PIL format
                            "input_tensor": false,
                            # we also produce output images in numpy/PIL format
                            "output_tensor": false,
                            "operations": {
                                # the augmentor pipeline defines two operations: rotations and flips
                                "rotate_random_90": {"probability": 0.75},
                                "flip_random": {"probability": 0.75}
                            }
                        }
                    }
                ]
            ],
            # the 'eval_augments' operations are applied to validation/test samples only
            "eval_augments": {
                # specifies whether to apply the augmentations before or after the base transforms
                "append": false,
                "transforms": [
                    # here, we use a combination of a sample duplicator and a random cropper; this
                    # increases the number of samples provided by the dataset parser
                    {
                        "operation": "thelper.transforms.Duplicator",
                        "parameters": {
                            "count": 10
                        }
                    },
                    {
                        "operation": "thelper.transforms.ImageTransformWrapper",
                        "parameters": {
                            "operation": "thelper.transforms.RandomResizedCrop",
                            "parameters": {
                                "output_size": [224, 224],
                                "input_size": [0.1, 1.0],
                                "ratio": 1.0
                            },
                            "force_convert": false
                        }
                    },
                ]
            ],
        # ...

    Args:
        config: the configuration dictionary defining the meta parameters as well as the list of transformation
            operations of the augmentation pipeline.

    Returns:
        A tuple that consists of a pipeline compatible with the ``torchvision.transforms`` interfaces, and
        a bool specifying whether this pipeline should be appended or prefixed to the base transforms.

    .. seealso::
        :class:`thelper.transforms.AugmentorWrapper`
        :func:`thelper.transforms.load_transforms`
        :func:`thelper.data.load`
    """
    if not isinstance(config, dict):
        raise AssertionError("augmentation config should be provided as dictionary")
    augments = None
    augments_append = False
    if "append" in config:
        augments_append = thelper.utils.str2bool(config["append"])
    if "transforms" in config and config["transforms"]:
        augments = thelper.transforms.load_transforms(config["transforms"])
    return augments, augments_append


class AugmentorWrapper(object):
    """Augmentor pipeline wrapper that allows pickling and multithreading.

    See https://github.com/mdbloice/Augmentor for more information. This wrapper was last updated to work
    with version 0.2.2 --- more recent versions introduced yet unfixed (as of 2018/08) issues on some platforms.

    All original transforms are supported here. This wrapper also fixes the list output bug for single-image
    samples when using operations individually.

    Attributes:
        pipeline: the augmentor pipeline instance to apply to images.

    .. seealso::
        :func:`thelper.transforms.load_transforms`
    """

    def __init__(self, pipeline):
        """Receives and stores an augmentor pipeline for later use.

        The pipeline itself is instantiated in :func:`thelper.transforms.load_transforms`.
        """
        self.pipeline = pipeline

    def __call__(self, samples):
        """Transforms a single image (or a list of images) using the augmentor pipeline.

        Args:
            samples: the image(s) to transform. Should be a numpy array or a PIL image (or a list of).

        Returns:
            The transformed image(s), with the same type as the input.
        """
        in_list = True
        if samples is not None and not isinstance(samples, list):
            in_list = False
            samples = [samples]
        elif not samples:
            return samples
        cvt_array = False
        if isinstance(samples[0], np.ndarray):
            for idx in range(len(samples)):
                samples[idx] = PIL.Image.fromarray(samples[idx])
            cvt_array = True
        elif not isinstance(samples[0], PIL.Image.Image):
            raise AssertionError("unexpected input sample type (must be np.ndarray or PIL.Image)")
        for operation in self.pipeline.operations:
            r = round(np.random.uniform(0, 1), 1)
            if r <= operation.probability:
                samples = operation.perform_operation(samples)
        if cvt_array:
            for idx in range(len(samples)):
                samples[idx] = np.asarray(samples[idx])
        if not in_list and len(samples) == 1:
            samples = samples[0]
        return samples

    def __repr__(self):
        """Create a print-friendly representation of inner augmentation stages."""
        return "AugmentorWrapper: " + str([str(op) for op in self.pipeline.operations])


class ImageTransformWrapper(object):
    """Image tranform wrapper that allows operations on lists.

    Can be used to wrap the operations in ``thelper.transforms`` or in ``torchvision.transforms``
    that only accept images as their input. Will optionally force-convert the images to PIL format.

    Can also be used to transform a list of images uniformly based on a shared dice roll.

    .. warning::
        Stochastic transforms (e.g. ``torchvision.transforms.RandomCrop``) will treat each image in
        a list differently. If the same operations are to be applied to all images, you should
        consider using a series non-stochastic operations wrapped inside an instance of
        ``torchvision.transforms.RandomApply``, or simply provide the probability of applying the
        transforms to this wrapper's constructor.

    Attributes:
        operation: the wrapped operation (callable object or class name string to import).
        parameters: the parameters that are passed to the operation when init'd or called.
        probability: the probability that the wrapped operation will be applied.
        force_convert: specifies whether images should be forced into PIL format or not.
    """

    def __init__(self, operation, parameters=None, probability=1, force_convert=True):
        """Receives and stores a torchvision transform operation for later use.

        If the operation is given as a string, it is assumed to be a class name and it will
        be imported. The parameters (if any) will then be given to the constructor of that
        class. Otherwise, the operation is assumed to be a callable object, and its parameters
        (if any) will be provided at call-time.

        Args:
            operation: the wrapped operation (callable object or class name string to import).
            parameters: the parameters that are passed to the operation when init'd or called.
            probability: the probability that the wrapped operation will be applied.
            force_convert: specifies whether images should be forced into PIL format or not.
        """
        if parameters is not None and not isinstance(parameters, dict):
            raise AssertionError("expected parameters to be passed in as a dictionary")
        if isinstance(operation, str):
            operation_type = thelper.utils.import_class(operation)
            self.operation = operation_type(**parameters) if parameters is not None else operation_type()
            self.parameters = {}
        else:
            self.operation = operation
            self.parameters = parameters if parameters is not None else {}
        if probability < 0 or probability > 1:
            raise AssertionError("invalid probability value (range is [0,1]")
        self.probability = probability
        self.force_convert = force_convert

    def __call__(self, samples):
        """Transforms a single image (or a list of images) using the torchvision operation.

        Args:
            samples: the image(s) to transform. Should be a numpy array or a PIL image (or a list of).

        Returns:
            The transformed image(s), with the same type as the input.
        """
        in_list = True
        if samples is not None and not isinstance(samples, list):
            in_list = False
            samples = [samples]
        elif not samples:
            return samples
        cvt_array = False
        if isinstance(samples[0], np.ndarray):
            if self.force_convert:
                # PIL is pretty bad, it can't handle 3-channel float images...
                # ... but hey, if your op only supports that, have fun!
                cvt_array = True
        elif not isinstance(samples[0], PIL.Image.Image):
            raise AssertionError("unexpected input sample type (must be np.ndarray or PIL.Image)")
        if self.probability >= 1 or round(np.random.uniform(0, 1), 1) <= self.probability:
            # we either apply the op on all samples, or on none
            for idx in range(len(samples)):
                if cvt_array:
                    samples[idx] = PIL.Image.fromarray(samples[idx])
                samples[idx] = self.operation(samples[idx], **self.parameters)
                if cvt_array:
                    samples[idx] = np.asarray(samples[idx])
        if not in_list and len(samples) == 1:
            samples = samples[0]
        return samples

    def __repr__(self):
        """Create a print-friendly representation of inner augmentation stages."""
        return "ImageTransformWrapper: [prob=%f: [%s]]" % (self.probability, str(self.operation))


class Compose(torchvision.transforms.Compose):
    """Composes several transforms together (with support for invert ops).

    This interface is fully compatible with ``torchvision.transforms.Compose``.
    """

    def __init__(self, transforms):
        """Forwards the list of transformations to the base class."""
        super().__init__(transforms)

    def invert(self, sample):
        """Tries to invert the transformations applied to a sample.

        Will throw if one of the transformations cannot be inverted.
        """
        for t in reversed(self.transforms):
            if not hasattr(t, "invert"):
                raise AssertionError("missing invert op for transform = %s" % repr(t))
            sample = t.invert(sample)
        return sample

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return "Compose: [\n" + ",\n".join([str(t) for t in self.transforms]) + "]"


class ToNumpy(object):
    """Converts and returns an image in numpy format from a ``torch.Tensor`` or ``PIL.Image`` format.

    This operation is deterministic. The returns image will always be encoded as HxWxC, where
    if the input has three channels, the ordering might be optionally changed.

    Attributes:
        reorder_BGR: specifies whether the channels should be reordered in OpenCV format.
    """

    def __init__(self, reorder_BGR=False):
        """Initializes transformation parameters."""
        self.reorder_BGR = reorder_BGR

    def __call__(self, sample):
        """Converts and returns an image in numpy format.

        Args:
            sample: the image to convert; should be a tensor, numpy array, or PIL image.

        Returns:
            The numpy-converted image.
        """
        if isinstance(sample, np.ndarray):
            pass  # no transform needed, channel reordering done at end
        elif isinstance(sample, torch.Tensor):
            sample = np.transpose(sample.cpu().numpy(), [1, 2, 0])  # CxHxW to HxWxC
        elif isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        else:
            raise AssertionError("unknown image type, cannot process sample")
        if self.reorder_BGR:
            return sample[..., ::-1]  # assumes channels already in last dim
        else:
            return sample

    def invert(self, sample):
        """Specifies that this operation cannot be inverted, as the original data type is unknown."""
        raise AssertionError("cannot be inverted")


class CenterCrop(object):
    """Returns a center crop from a given image via OpenCV and numpy.

    This operation is deterministic. The code relies on OpenCV, meaning the border arguments
    must be compatible with ``cv2.copyMakeBorder``.

    Attributes:
        size: the size of the target crop (tuple of width, height).
        relative: specifies whether the target crop size is relative to the image size or not.
        bordertype: argument forwarded to ``cv2.copyMakeBorder``.
        borderval: argument forwarded to ``cv2.copyMakeBorder``.
    """

    def __init__(self, size, bordertype=cv.BORDER_CONSTANT, borderval=0):
        """Validates and initializes center crop parameters.

        Args:
            size: size of the target crop, provided as tuple or list. If integer values are used,
                the size is assumed to be absolute. If floating point values are used, the size is
                assumed to be relative, and will be determined dynamically for each sample. If a
                tuple is used, it is assumed to be (width, height).
            bordertype: border copy type to use when the image is too small for the required crop size.
                See ``cv2.copyMakeBorder`` for more information.
            borderval: border value to use when the image is too small for the required crop size. See
                ``cv2.copyMakeBorder`` for more information.
        """
        if isinstance(size, (tuple, list)):
            if len(size) != 2:
                raise AssertionError("expected center crop dim input as 2-item tuple (width,height)")
            self.size = tuple(size)
        elif isinstance(size, (int, float)):
            self.size = (size, size)
        else:
            raise AssertionError("unexpected center crop dim input type (need tuple or int/float)")
        if self.size[0] <= 0 or self.size[1] <= 0:
            raise AssertionError("crop dimensions must be positive")
        if isinstance(self.size[0], float):
            self.relative = True
        elif isinstance(self.size[0], int):
            self.relative = False
        else:
            raise AssertionError("unexpected center crop dim input type (need tuple or int/float)")
        self.bordertype = thelper.utils.import_class(bordertype) if isinstance(bordertype, str) else bordertype
        self.borderval = borderval

    def __call__(self, sample):
        """Extracts and returns a central crop from the provided image.

        Args:
            sample: the image to generate the crop from; should be a 2d or 3d numpy array.

        Returns:
            The center crop.
        """
        if sample.ndim < 2 or sample.ndim > 3:
            raise AssertionError("bad input dimensions; must be 2-d, or 3-d (with channels)")
        crop_height = int(round(self.size[1] * sample.shape[0])) if self.relative else self.size[1]
        crop_width = int(round(self.size[0] * sample.shape[1])) if self.relative else self.size[0]
        tl = [sample.shape[1] // 2 - crop_width // 2, sample.shape[0] // 2 - crop_height // 2]
        br = [tl[0] + crop_width, tl[1] + crop_height]
        return thelper.utils.safe_crop(sample, tl, br, self.bordertype, self.borderval)

    def invert(self, sample):
        """Specifies that this operation cannot be inverted, as data loss is incurred during image transformation."""
        raise AssertionError("cannot be inverted")

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(size={0}, bordertype={1}, bordervalue={2})".format(self.size, self.bordertype, self.borderval)


class RandomResizedCrop(object):
    """Returns a resized crop of a randomly selected image region.

    This operation is stochastic, and thus cannot be inverted. Each time the operation is called,
    a random check will determine whether a transformation is applied or not. The code relies on
    OpenCV, meaning the interpolation arguments must be compatible with ``cv2.resize``.

    Attributes:
        output_size: size of the output crop, provided as a single element (``edge_size``) or as a
            two-element tuple or list (``[width, height]``). If integer values are used, the size is
            assumed to be absolute. If floating point values are used (i.e. in [0,1]), the output
            size is assumed to be relative to the original image size, and will be determined at
            execution time for each sample.
        input_size: range of the input region sizes, provided as a pair of elements
            (``[min_edge_size, max_edge_size]``) or as a pair of tuples or lists
            (``[[min_width, min_height], [max_width, max_height]]``). If the pair-of-pairs format is
            used, the ``ratio`` argument cannot be used. If integer values are used, the ranges are
            assumed to be absolute. If floating point values are used (i.e. in [0,1]), the ranges
            are assumed to be relative to the original image size, and will be determined at
            execution time for each sample.
        ratio: range of minimum/maximum input region aspect ratios to use. This argument cannot be
            used if the pair-of-pairs format is used for the ``input_size`` argument.
        probability: the probability that the transformation will be applied when called; if not
            applied, the returned image will be the original.
        random_attempts: the number of random sampling attempts to try before reverting to center
            or most-probably-valid crop generation.
        flags: interpolation flag forwarded to ``cv2.resize``.
    """

    def __init__(self, output_size, input_size=(0.08, 1.0), ratio=(0.75, 1.33),
                 probability=1.0, random_attempts=10, flags=cv.INTER_LINEAR):
        """Validates and initializes center crop parameters.

        Args:
            output_size: size of the output crop, provided as a single element (``edge_size``) or as a
                two-element tuple or list (``[width, height]``). If integer values are used, the size is
                assumed to be absolute. If floating point values are used (i.e. in [0,1]), the output
                size is assumed to be relative to the original image size, and will be determined at
                execution time for each sample.
            input_size: range of the input region sizes, provided as a pair of elements
                (``[min_edge_size, max_edge_size]``) or as a pair of tuples or lists
                (``[[min_width, min_height], [max_width, max_height]]``). If the pair-of-pairs format is
                used, the ``ratio`` argument cannot be used. If integer values are used, the ranges are
                assumed to be absolute. If floating point values are used (i.e. in [0,1]), the ranges
                are assumed to be relative to the original image size, and will be determined at
                execution time for each sample.
            ratio: range of minimum/maximum input region aspect ratios to use. This argument cannot be
                used if the pair-of-pairs format is used for the ``input_size`` argument.
            probability: the probability that the transformation will be applied when called; if not
                applied, the returned image will be the original.
            random_attempts: the number of random sampling attempts to try before reverting to center
                or most-probably-valid crop generation.
            flags: interpolation flag forwarded to ``cv2.resize``.
        """
        if isinstance(output_size, (tuple, list)):
            if len(output_size) != 2:
                raise AssertionError("expected output size to be two-element list or tuple, or single scalar")
            if not all([isinstance(s, int) for s in output_size]) and not all([isinstance(s, float) for s in output_size]):
                raise AssertionError("expected output size pair elements to be the same type (int or float)")
            self.output_size = output_size
        elif isinstance(output_size, (int, float)):
            self.output_size = (output_size, output_size)
        else:
            raise AssertionError("unexpected output size type (need tuple/list/int/float)")
        for s in self.output_size:
            if not ((isinstance(s, float) and 0 < s <= 1) or (isinstance(s, int) and s > 0)):
                raise AssertionError("invalid output size value (%s)" % str(s))
        if not isinstance(input_size, (tuple, list)) or len(input_size) != 2:
            raise AssertionError("expected input size to be provided as a pair of elements or a pair of tuples/lists")
        if all([isinstance(s, int) for s in input_size]) or all([isinstance(s, float) for s in input_size]):
            if ratio is not None and isinstance(ratio, (tuple, list)):
                if len(ratio) != 2:
                    raise AssertionError("invalid ratio tuple/list length (expected two elements)")
                if not all([isinstance(r, float) for r in ratio]):
                    raise AssertionError("expected ratio pair elements to be float values")
                self.ratio = (min(ratio), max(ratio))
            elif ratio is not None and isinstance(ratio, float):
                self.ratio = (ratio, ratio)
            else:
                raise AssertionError("invalid aspect ratio, expected 2-element tuple/list or single float")
            self.input_size = ((min(input_size), min(input_size)), (max(input_size), max(input_size)))
        elif all([isinstance(s, tuple) and len(s) == 2 for s in input_size]) or \
                all([isinstance(s, list) and len(s) == 2 for s in input_size]):
            if ratio is not None and isinstance(ratio, (tuple, list)) and len(ratio) != 0:
                raise AssertionError("cannot specify input sizes in two-element tuples/lists and also provide aspect ratios")
            for t in input_size:
                for s in t:
                    if not isinstance(s, (int, float)) or isinstance(type(s), type(input_size[0][0])):
                        raise AssertionError("input sizes should all be same type, either int or float")
            self.input_size = (min(input_size[0][0], input_size[1][0]), min(input_size[0][1], input_size[1][1]),
                               max(input_size[0][0], input_size[1][0]), max(input_size[0][1], input_size[1][1]))
            self.ratio = None  # ignored since input_size contains all necessary info
        else:
            raise AssertionError("expected input size to be two-elem list/tuple of int/float or two-element list/tuple of int/float")
        for t in self.input_size:
            for s in t:
                if not ((isinstance(s, float) and 0 < s <= 1) or (isinstance(s, int) and s > 0)):
                    raise AssertionError("invalid input size value (%s)" % str(s))
        if probability < 0 or probability > 1:
            raise AssertionError("invalid probability value (should be in [0,1]")
        self.probability = probability
        if random_attempts <= 0:
            raise AssertionError("invalid random_attempts value (should be > 0)")
        self.random_attempts = random_attempts
        self.flags = thelper.utils.import_class(flags) if isinstance(flags, str) else flags

    def __call__(self, sample):
        """Extracts and returns a random (resized) crop from the provided image.

        Args:
            sample: the image to generate the crop from; should be a 2d or 3d numpy array.

        Returns:
            The randomly selected and resized crop.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        if self.probability < 1 and np.random.uniform(0, 1) > self.probability:
            return sample
        image_height, image_width = sample.shape[0], sample.shape[1]
        target_height, target_width = None, None
        target_row, target_col = None, None
        for attempt in range(self.random_attempts):
            if self.ratio is None:
                target_width = np.random.uniform(self.input_size[0][0], self.input_size[1][0])
                target_height = np.random.uniform(self.input_size[0][1], self.input_size[1][1])
                if isinstance(self.input_size[0][0], float):
                    target_width *= image_width
                    target_height *= image_height
                target_width = int(round(target_width))
                target_height = int(round(target_height))
            elif isinstance(self.input_size[0][0], (int, float)):
                if isinstance(self.input_size[0][0], float):
                    area = image_height * image_width
                    target_area = np.random.uniform(self.input_size[0][0], self.input_size[1][0]) * area
                else:
                    target_area = np.random.uniform(self.input_size[0][0], self.input_size[1][0]) ** 2
                aspect_ratio = np.random.uniform(*self.ratio)
                target_width = int(round(math.sqrt(target_area * aspect_ratio)))
                target_height = int(round(math.sqrt(target_area / aspect_ratio)))
                if np.random.random() < 0.5:
                    target_width, target_height = target_height, target_width
            else:
                raise AssertionError("unhandled crop strategy")
            if target_width <= image_width and target_height <= image_height:
                target_col = np.random.randint(min(0, image_width - target_width), max(0, image_width - target_width) + 1)
                target_row = np.random.randint(min(0, image_height - target_height), max(0, image_height - target_height) + 1)
                break
        if image_height is None or image_width is None:
            # fallback, use centered crop
            target_width = target_height = min(sample.shape[0], sample.shape[1])
            target_col = (sample.shape[1] - target_width) // 2
            target_row = (sample.shape[0] - target_height) // 2
        crop = thelper.utils.safe_crop(sample, (target_col, target_row), (target_col + target_width, target_row + target_height))
        if isinstance(self.output_size[0], float):
            output_width = int(round(self.output_size[0] * sample.shape[1]))
            output_height = int(round(self.output_size[1] * sample.shape[0]))
            return cv.resize(crop, (output_width, output_height), interpolation=self.flags)
        elif isinstance(self.output_size[0], int):
            return cv.resize(crop, (self.output_size[0], self.output_size[1]), interpolation=self.flags)
        else:
            raise AssertionError("unhandled crop strategy")

    def invert(self, sample):
        """Specifies that this operation cannot be inverted, as data loss is incurred during image transformation."""
        raise AssertionError("cannot be inverted")

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        format_str = self.__class__.__name__ + "("
        format_str += "output_size={}, ".format(self.output_size)
        format_str += "input_size={}, ".format(self.input_size)
        format_str += "ratio={}, ".format(self.ratio)
        format_str += "probability={}, ".format(self.probability)
        format_str += "random_attempts={}, ".format(self.random_attempts)
        format_str += "flags={})".format(self.flags)
        return format_str


class Resize(object):
    """Resizes a given image using OpenCV and numpy.

    This operation is deterministic. The code relies on OpenCV, meaning the interpolation arguments
    must be compatible with ``cv2.resize``.

    Attributes:
        interp: interpolation type to use (forwarded to ``cv2.resize``)
        buffer: specifies whether a destination buffer should be used to avoid allocations
        dsize: target image size (tuple of width, height).
        dst: buffer used to avoid reallocations if ``self.buffer == True``
        fx: argument forwarded to ``cv2.resize``.
        fy: argument forwarded to ``cv2.resize``.
    """

    def __init__(self, dsize, fx=0, fy=0, interp=cv.INTER_LINEAR, buffer=False):
        """Validates and initializes resize parameters.

        Args:
            dsize: size of the target image, forwarded to ``cv2.resize``.
            fx: x-scaling factor, forwarded to ``cv2.resize``.
            fy: y-scaling factor, forwarded to ``cv2.resize``.
            interp: resize interpolation type, forwarded to ``cv2.resize``.
            buffer: specifies whether a destination buffer should be used to avoid allocations
        """
        self.interp = thelper.utils.import_class(interp) if isinstance(interp, str) else interp
        self.buffer = buffer
        if isinstance(dsize, tuple):
            self.dsize = dsize
        else:
            self.dsize = tuple(dsize)
        self.dst = None
        self.fx = fx
        self.fy = fy
        if fx < 0 or fy < 0:
            raise AssertionError("scale factors should be null (ignored) or positive")
        if dsize[0] < 0 or dsize[1] < 0:
            raise AssertionError("destination image size should be null (ignored) or positive")
        if fx == 0 and fy == 0 and dsize[0] == 0 and dsize[1] == 0:
            raise AssertionError("need to specify either destination size or scale factor(s)")

    def __call__(self, sample):
        """Returns a resized copy of the provided image.

        Args:
            sample: the image to resize; should be a 2d or 3d numpy array.

        Returns:
            The resized image. May be allocated on the spot, or be a pointer to a local buffer.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        if sample.ndim < 2 or sample.ndim > 3:
            raise AssertionError("bad input dimensions; must be 2-d, or 3-d (with channels)")
        if sample.ndim < 3 or sample.shape[2] <= 4:
            if self.buffer:
                cv.resize(sample, self.dsize, dst=self.dst, fx=self.fx, fy=self.fy, interpolation=self.interp)
                if self.dst.ndim == 2:
                    return np.expand_dims(self.dst, 2)
                return self.dst
            else:
                dst = cv.resize(sample, self.dsize, fx=self.fx, fy=self.fy, interpolation=self.interp)
                if dst.ndim == 2:
                    dst = np.expand_dims(dst, 2)
                return dst
        else:  # too many channels, need to split-resize
            slices_dst = self.dst if self.buffer else None
            slices = np.split(sample, sample.shape[2], 2)
            if slices_dst is None or not isinstance(slices_dst, list) or len(slices_dst) != len(slices):
                slices_dst = [None] * len(slices)
            for idx in range(len(slices)):
                cv.resize(slice[idx], self.dsize, dst=slices_dst[idx], fx=self.fx, fy=self.fy, interpolation=self.interp)
            if self.buffer:
                self.dst = slices_dst
            return np.stack(slices_dst, 2)

    def invert(self, sample):
        """Specifies that this operation cannot be inverted, as data loss is incurred during image transformation."""
        # todo, could implement if original size is fixed & known
        raise AssertionError("missing implementation")

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(dsize={0}, fx={1}, fy={2}, interp={3})".format(self.dsize, self.fx, self.fy, self.interp)


class Affine(object):
    """Warps a given image using an affine matrix via OpenCV and numpy.

    This operation is deterministic. The code relies on OpenCV, meaning the border arguments
    must be compatible with ``cv2.warpAffine``.

    Attributes:
        transf: the 2x3 transformation matrix passed to ``cv2.warpAffine``.
        out_size: target image size (tuple of width, height). If None, same as original.
        flags: extra warp flags forwarded to ``cv2.warpAffine``.
        border_mode: border extrapolation mode forwarded to ``cv2.warpAffine``.
        border_val: border constant extrapolation value forwarded to ``cv2.warpAffine``.
    """

    def __init__(self, transf, out_size=None, flags=None, border_mode=None, border_val=None):
        """Validates and initializes affine warp parameters.

        Args:
            transf: the 2x3 transformation matrix passed to ``cv2.warpAffine``.
            out_size: target image size (tuple of width, height). If None, same as original.
            flags: extra warp flags forwarded to ``cv2.warpAffine``.
            border_mode: border extrapolation mode forwarded to ``cv2.warpAffine``.
            border_val: border constant extrapolation value forwarded to ``cv2.warpAffine``.
        """
        if isinstance(transf, np.ndarray):
            if transf.size != 6:
                raise AssertionError("transformation matrix must be 2x3")
            self.transf = transf.reshape((2, 3)).astype(np.float32)
        elif isinstance(transf, list):
            if not len(transf) == 6:
                raise AssertionError("transformation matrix must be 6 elements (2x3)")
            self.transf = np.asarray(transf).reshape((2, 3)).astype(np.float32)
        else:
            raise AssertionError("unexpected transformation matrix type")
        self.out_size = None
        if out_size is not None:
            if isinstance(out_size, list):
                if len(out_size) != 2:
                    raise AssertionError("output image size should be 2-elem list or tuple")
                self.out_size = tuple(out_size)
            elif isinstance(out_size, tuple):
                if len(out_size) != 2:
                    raise AssertionError("output image size should be 2-elem list or tuple")
                self.out_size = out_size
            else:
                raise AssertionError("unexpected output size type")
        self.flags = flags
        if self.flags is None:
            self.flags = cv.INTER_LINEAR
        self.border_mode = border_mode
        if self.border_mode is None:
            self.border_mode = cv.BORDER_CONSTANT
        self.border_val = border_val
        if self.border_val is None:
            self.border_val = 0

    def __call__(self, sample):
        """Warps a given image using an affine matrix.

        Args:
            sample: the image to warp; should be a 2d or 3d numpy array.

        Returns:
            The warped image.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        out_size = self.out_size
        if out_size is None:
            out_size = (sample.shape[1], sample.shape[0])
        return cv.warpAffine(sample, self.transf, dsize=out_size, flags=self.flags,
                             borderMode=self.border_mode, borderValue=self.border_val)

    def invert(self, sample):
        """Inverts the warp transformation, but only is the output image has not been cropped before."""
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        out_size = self.out_size
        if out_size is None:
            out_size = (sample.shape[1], sample.shape[0])
        else:
            raise AssertionError("unknown original image size, cannot invert affine transform")
        return cv.warpAffine(sample, self.transf, dsize=out_size, flags=self.flags ^ cv.WARP_INVERSE_MAP,
                             borderMode=self.border_mode, borderValue=self.border_val)

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(transf={0}, out_size={1})".format(np.array2string(self.transf), self.out_size)


class RandomShift(object):
    """Randomly translates an image in a provided range via OpenCV and numpy.

    This operation is stochastic, and thus cannot be inverted. Each time the operation is called,
    a random check will determine whether a transformation is applied or not. The code relies on
    OpenCV, meaning the border arguments must be compatible with ``cv2.warpAffine``.

    Attributes:
        min: the minimum pixel shift that can be applied stochastically.
        max: the maximum pixel shift that can be applied stochastically.
        probability: the probability that the transformation will be applied when called.
        flags: extra warp flags forwarded to ``cv2.warpAffine``.
        border_mode: border extrapolation mode forwarded to ``cv2.warpAffine``.
        border_val: border constant extrapolation value forwarded to ``cv2.warpAffine``.
    """

    def __init__(self, min, max, probability=1.0, flags=None, border_mode=None, border_val=None):
        """Validates and initializes shift parameters.

        Args:
            min: the minimum pixel shift that can be applied stochastically.
            max: the maximum pixel shift that can be applied stochastically.
            probability: the probability that the transformation will be applied when called.
            flags: extra warp flags forwarded to ``cv2.warpAffine``.
            border_mode: border extrapolation mode forwarded to ``cv2.warpAffine``.
            border_val: border constant extrapolation value forwarded to ``cv2.warpAffine``.
        """
        if isinstance(min, tuple) and isinstance(max, tuple):
            if len(min) != len(max) or len(min) != 2:
                raise AssertionError("min/max shift tuple must be 2-elem")
            self.min = min
            self.max = max
        elif isinstance(min, list) and isinstance(max, list):
            if len(min) != len(max) or len(min) != 2:
                raise AssertionError("min/max shift list must be 2-elem")
            self.min = tuple(min)
            self.max = tuple(max)
        elif isinstance(min, (int, float)) and isinstance(max, (int, float)):
            self.min = (min, min)
            self.max = (max, max)
        else:
            raise AssertionError("unexpected min/max combo types")
        if self.max[0] < self.min[0] or self.max[1] < self.min[1]:
            raise AssertionError("bad min/max values")
        if probability < 0 or probability > 1:
            raise AssertionError("bad probability range")
        self.probability = probability
        self.flags = flags
        if self.flags is None:
            self.flags = cv.INTER_LINEAR
        self.border_mode = border_mode
        if self.border_mode is None:
            self.border_mode = cv.BORDER_CONSTANT
        self.border_val = border_val
        if self.border_val is None:
            self.border_val = 0

    def __call__(self, sample):
        """Translates a given image using a predetermined min/max range.

        Args:
            sample: the image to translate; should be a 2d or 3d numpy array.

        Returns:
            The translated image.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        if self.probability < 1 and np.random.uniform(0, 1) > self.probability:
            return sample
        out_size = (sample.shape[1], sample.shape[0])
        x_shift = np.random.uniform(self.min[0], self.max[0])
        y_shift = np.random.uniform(self.min[1], self.max[1])
        transf = np.float32([[1, 0, x_shift], [0, 1, y_shift]])
        return cv.warpAffine(sample, transf, dsize=out_size, flags=self.flags, borderMode=self.border_mode, borderValue=self.border_val)

    def invert(self, sample):
        """Specifies that this operation cannot be inverted, as it is stochastic, and data loss occurs during transformation."""
        raise AssertionError("operation cannot be inverted")

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(min={0}, max={1}, prob={2})".format(self.min, self.max, self.probability)


class Transpose(object):
    """Transposes an image via numpy.

    This operation is deterministic.

    Attributes:
        axes: the axes on which to apply the transpose; forwarded to ``numpy.transpose``.
        axes_inv: used to invert the tranpose; also forwarded to ``numpy.transpose``.
    """

    def __init__(self, axes):
        """Validates and initializes tranpose parameters.

        Args:
            axes: the axes on which to apply the transpose; forwarded to ``numpy.transpose``.
        """
        axes = np.asarray(axes)
        if axes.ndim > 1:
            raise AssertionError("tranpose param should be 1-d")
        if np.any(axes >= axes.size):
            raise AssertionError("oob dim in axes")
        self.axes = axes
        self.axes_inv = np.asarray([None] * axes.size)
        for i in list(range(len(axes))):
            self.axes_inv[self.axes[i]] = i

    def __call__(self, sample):
        """Transposes a given image.

        Args:
            sample: the image to transpose; should be a numpy array.

        Returns:
            The transposed image.
        """
        return np.transpose(sample, self.axes)

    def invert(self, sample):
        """Invert-transposes a given image.

        Args:
            sample: the image to invert-transpose; should be a numpy array.

        Returns:
            The invert-transposed image.
        """
        return np.transpose(sample, self.axes_inv)

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(axes={0})".format(self.axes)


class Duplicator(object):
    """Duplicates and returns a list of copies of the input sample.

    This operation is used in data augmentation pipelines that rely on probabilistic or preset transformations.
    It can produce a fixed number of simple copies or deep copies of the input samples as required.

    Attributes:
        count: number of copies to generate.
        deepcopy: specifies whether to deep-copy samples or not.
    """

    def __init__(self, count, deepcopy=False):
        """Validates and initializes duplication parameters.

        Args:
            count: number of copies to generate.
            deepcopy: specifies whether to deep-copy samples or not.
        """
        if count <= 0:
            raise AssertionError("invalid copy count")
        self.count = count
        self.deepcopy = deepcopy

    def __call__(self, sample):
        """Generates and returns a list of duplicates.

        Args:
            sample: the sample to duplicate.

        Returns:
            A list of duplicated samples.
        """
        duplicates = []
        for idx in range(self.count):
            if self.deepcopy:
                duplicates.append(copy.deepcopy(sample))
            else:
                duplicates.append(copy.copy(sample))
        return duplicates

    def invert(self, sample):
        """Returns the first instance of the list of duplicates."""
        if not isinstance(sample, list):
            raise AssertionError("invalid sample type (should be list)")
        if len(sample) != self.count:
            raise AssertionError("invalid sample list length")
        return sample[0]

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + \
            "(count={0}, deepcopy={1})".format(self.count, self.deepcopy)


class Tile(object):
    """Returns a list of tiles cut out from a given image.

    This operation can perform tiling given an optional mask with a target intersection over union (IoU) score,
    and with an optional overlap between tiles. The tiling is deterministic and can thus be inverted, but only
    if a mask is not used, as some image regions may be lost otherwise.

    If a mask is used, the first tile position is tested exhaustively by iterating over all input coordinates
    starting from the top-left corner of the image. Otherwise, the first tile position is set as (0,0). Then,
    all other tiles are found by offsetting frm these coordinates, and testing for IoU with the mask (if needed).

    Attributes:
        tile_size: size of the output tiles, provided as a single element (``edge_size``) or as a
            two-element tuple or list (``[width, height]``). If integer values are used, the size is
            assumed to be absolute. If floating point values are used (i.e. in [0,1]), the output
            size is assumed to be relative to the original image size, and will be determined at
            execution time for each image.
        tile_overlap: overlap allowed between two neighboring tiles; should be a ratio in [0,1].
        min_mask_iou: minimum mask intersection over union (IoU) required for accepting a tile (in [0,1]).
        offset_overlap: specifies whether the overlap tiling should be offset outside the image or not.
        bordertype: border copy type to use when the image is too small for the required crop size.
            See ``cv2.copyMakeBorder`` for more information.
        borderval: border value to use when the image is too small for the required crop size. See
            ``cv2.copyMakeBorder`` for more information.
    """

    def __init__(self, tile_size, tile_overlap=0.0, min_mask_iou=1.0, offset_overlap=False, bordertype=cv.BORDER_CONSTANT, borderval=0):
        """Validates and initializes tiling parameters.

        Args:
            tile_size: size of the output tiles, provided as a single element (``edge_size``) or as a
                two-element tuple or list (``[width, height]``). If integer values are used, the size is
                assumed to be absolute. If floating point values are used (i.e. in [0,1]), the output
                size is assumed to be relative to the original image size, and will be determined at
                execution time for each image.
            tile_overlap: overlap ratio between two consecutive (neighboring) tiles; should be in [0,1].
            min_mask_iou: minimum mask intersection over union (IoU) required for producing a tile.
            offset_overlap: specifies whether the overlap tiling should be offset outside the image or not.
            bordertype: border copy type to use when the image is too small for the required crop size.
                See ``cv2.copyMakeBorder`` for more information.
            borderval: border value to use when the image is too small for the required crop size. See
                ``cv2.copyMakeBorder`` for more information.
        """
        if isinstance(tile_size, (tuple, list)):
            if len(tile_size) != 2:
                raise AssertionError("expected tile size to be two-element list or tuple, or single scalar")
            if not all([isinstance(s, int) for s in tile_size]) and not all([isinstance(s, float) for s in tile_size]):
                raise AssertionError("expected tile size pair elements to be the same type (int or float)")
            self.tile_size = tile_size
        elif isinstance(tile_size, (int, float)):
            self.tile_size = (tile_size, tile_size)
        else:
            raise AssertionError("unexpected tile size type (need tuple/list/int/float)")
        if not isinstance(tile_overlap, float) or tile_overlap < 0 or tile_overlap >= 1:
            raise AssertionError("invalid tile overlap (should be float in [0,1[)")
        self.tile_overlap = tile_overlap
        if not isinstance(min_mask_iou, float) or min_mask_iou < 0 or tile_overlap > 1:
            raise AssertionError("invalid minimum mask IoU score (should be float in [0,1])")
        self.min_mask_iou = min_mask_iou
        self.offset_overlap = offset_overlap
        self.bordertype = thelper.utils.import_class(bordertype) if isinstance(bordertype, str) else bordertype
        self.borderval = borderval

    def __call__(self, image, mask=None):
        """Extracts and returns a list of tiles cut out from the given image.

        Args:
            image: the image to cut into tiles.
            mask: the mask to check tile intersections with (may be ``None``).

        Returns:
            A list of tiles (numpy-compatible images).
        """
        tile_rects, tile_images = self._get_tile_rects(image, mask), []
        for rect in tile_rects:
            tile_images.append(thelper.utils.safe_crop(image, (rect[0], rect[1]),
                                                       (rect[0]+rect[2], rect[1]+rect[3]),
                                                       self.bordertype, self.borderval))
        return tile_images

    def count_tiles(self, image, mask=None):
        """Returns the number of tiles that would be cut out from the given image.

        Args:
            image: the image to cut into tiles.
            mask: the mask to check tile intersections with (may be ``None``).

        Returns:
            The number of tiles that would be cut with :func:`thelper.transforms.Tile.__call__`.
        """
        return len(self._get_tile_rects(image, mask))

    def _get_tile_rects(self, image, mask=None):
        if isinstance(image, PIL.Image.Image):
            image = np.asarray(image)
        if mask is not None and isinstance(mask, PIL.Image.Image):
            mask = np.asarray(mask)
        tile_rects = []
        height, width = image.shape[0], image.shape[1]
        if isinstance(self.tile_size[0], float):
            tile_size = (int(round(self.tile_size[0] * width)), int(round(self.tile_size[1] * height)))
        else:
            tile_size = self.tile_size
        overlap = (int(round(tile_size[0] * self.tile_overlap)), int(round(tile_size[1] * self.tile_overlap)))
        overlap_offset = (-overlap[0] // 2, -overlap[1] // 2) if self.offset_overlap else (0, 0)
        step_size = (max(tile_size[0] - (overlap[0] // 2) * 2, 1), max(tile_size[1] - (overlap[1] // 2) * 2, 1))
        req_mask_area = tile_size[0] * tile_size[1] * self.min_mask_iou
        if mask is not None:
            if height != mask.shape[0] or width != mask.shape[1]:
                raise AssertionError("image and mask dimensions mismatch")
            if mask.ndim != 2:
                raise AssertionError("mask should be 2d binary (uchar) array")
            offset_coord = None
            row_range = range(overlap_offset[1], height - overlap_offset[1] - tile_size[1] + 1)
            col_range = range(overlap_offset[0], width - overlap_offset[0] - tile_size[0] + 1)
            for row, col in itertools.product(row_range, col_range):
                crop = thelper.utils.safe_crop(mask, (col, row), (col + tile_size[0], row + tile_size[1]))
                if np.count_nonzero(crop) >= req_mask_area:
                    offset_coord = (overlap_offset[0] + ((col - overlap_offset[0]) % step_size[0]),
                                    overlap_offset[1] + ((row - overlap_offset[1]) % step_size[1]))
                    break
            if offset_coord is None:
                return tile_rects
        else:
            offset_coord = overlap_offset
        row = offset_coord[1]
        while row + tile_size[1] <= height - overlap_offset[1]:
            col = offset_coord[0]
            while col + tile_size[0] <= width - overlap_offset[0]:
                if mask is not None:
                    crop = thelper.utils.safe_crop(mask, (col, row), (col + tile_size[0], row + tile_size[1]))
                    if np.count_nonzero(crop) >= req_mask_area:
                        tile_rects.append((col, row, tile_size[0], tile_size[1]))  # rect = (x, y, w, h)
                else:
                    tile_rects.append((col, row, tile_size[0], tile_size[1]))  # rect = (x, y, w, h)
                col += step_size[0]
            row += step_size[1]
        return tile_rects

    def invert(self, image, mask=None):
        """Returns the reconstituted image from a list of tiles, or throws if a mask was used."""
        if mask is not None:
            raise AssertionError("cannot invert operation, mask might have forced the loss of image content")
        else:
            raise NotImplementedError  # TODO

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + \
            "(tsize={0}, overlap={1}, iou={2})".format(self.tile_size, self.tile_overlap, self.min_mask_iou)


class NormalizeZeroMeanUnitVar(object):
    """Normalizes a given image using a set of mean and standard deviation parameters.

    The samples will be transformed such that ``s = (s - mean) / std``.

    This can be used for whitening; see https://en.wikipedia.org/wiki/Whitening_transformation
    for more information. Note that this operation is also not restricted to images.

    Attributes:
        mean: an array of mean values to subtract from data samples.
        std: an array of standard deviation values to divide with.
        out_type: the output data type to cast the normalization result to.
    """

    def __init__(self, mean, std, out_type=np.float32):
        """Validates and initializes normalization parameters.

        Args:
            mean: an array of mean values to subtract from data samples.
            std: an array of standard deviation values to divide with.
            out_type: the output data type to cast the normalization result to.
        """
        self.out_type = out_type
        self.mean = np.asarray(mean).astype(out_type)
        self.std = np.asarray(std).astype(out_type)
        if self.mean.ndim != 1 or self.std.ndim != 1:
            raise AssertionError("normalization params should be 1-d")
        if self.mean.size != self.std.size:
            raise AssertionError("normalization params size mismatch")
        if any([d == 0 for d in self.std]):
            raise AssertionError("normalization std must be non-null")

    def __call__(self, sample):
        """Normalizes a given sample.

        Args:
            sample: the sample to normalize. If given as a PIL image, it
            will be converted to a numpy array first.

        Returns:
            The warped sample, in a numpy array of type ``self.out_type``.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        return ((sample - self.mean) / self.std).astype(self.out_type)

    def invert(self, sample):
        """Inverts the normalization."""
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        return (sample * self.std + self.mean).astype(self.out_type)

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(mean={0}, std={1}, out_type={2})".format(self.mean, self.std, self.out_type)


class NormalizeMinMax(object):
    """Normalizes a given image using a set of minimum and maximum values.

    The samples will be transformed such that ``s = (s - min) / (max - min)``.

    Note that this operation is also not restricted to images.

    Attributes:
        min: an array of minimum values to subtract with.
        max: an array of maximum values to divide with.
        out_type: the output data type to cast the normalization result to.
    """

    def __init__(self, min, max, out_type=np.float32):
        """Validates and initializes normalization parameters.

        Args:
            min: an array of minimum values to subtract with.
            max: an array of maximum values to divide with.
            out_type: the output data type to cast the normalization result to.
        """
        self.out_type = out_type
        self.min = np.asarray(min).astype(out_type)
        if self.min.ndim == 0:
            self.min = np.expand_dims(self.min, 0)
        self.max = np.asarray(max).astype(out_type)
        if self.max.ndim == 0:
            self.max = np.expand_dims(self.max, 0)
        if self.min.ndim != 1 or self.max.ndim != 1:
            raise AssertionError("normalization params should be a 1-d array (one value per channel)")
        if self.min.size != self.max.size:
            raise AssertionError("normalization params size mismatch")
        self.diff = self.max - self.min
        if any([d == 0 for d in self.diff]):
            raise AssertionError("normalization diff must be non-null")

    def __call__(self, sample):
        """Normalizes a given sample.

        Args:
            sample: the sample to normalize. If given as a PIL image, it
            will be converted to a numpy array first.

        Returns:
            The warped sample, in a numpy array of type ``self.out_type``.
        """
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        return ((sample - self.min) / self.diff).astype(self.out_type)

    def invert(self, sample):
        """Inverts the normalization."""
        if isinstance(sample, PIL.Image.Image):
            sample = np.asarray(sample)
        return (sample * self.diff + self.min).astype(self.out_type)

    def __repr__(self):
        """Provides print-friendly output for class attributes."""
        return self.__class__.__name__ + "(min={0}, max={1}, out_type={2})".format(self.min, self.max, self.out_type)
