"""Common object interfaces module.

The interfaces defined here are fairly generic and used to eliminate
issues related to circular module importation.
"""

import abc
import collections
import copy
import logging
import os
import pprint
import typing

import numpy as np

import thelper.concepts
import thelper.typedefs
import thelper.utils

logger = logging.getLogger(__name__)


class PredictionConsumer(abc.ABC):
    """Abstract model prediction consumer class.

    This interface defines basic functions required so that :class:`thelper.train.base.Trainer` can
    figure out how to instantiate and update a model prediction consumer. The most notable class derived
    from this interface is :class:`thelper.optim.metrics.Metric` which is used to monitor the
    improvement of a model during a training session. Other prediction consumers defined in
    :mod:`thelper.train.utils` will instead log predictions to local files, create graphs, etc.
    """

    def __repr__(self) -> str:
        """Returns a generic print-friendly string containing info about this consumer."""
        return self.__class__.__module__ + "." + self.__class__.__qualname__ + "()"

    def reset(self) -> None:
        """Resets the internal state of the consumer.

        May be called for example by the trainer between two evaluation epochs. The default implementation
        does nothing, and if a reset behavior is needed, it should be implemented by the derived class.
        """
        pass

    @abc.abstractmethod
    def update(
        self,  # see `thelper.typedefs.IterCallbackParams` for more info
        task: "thelper.tasks.Task",
        input: thelper.typedefs.InputType,
        pred: thelper.typedefs.AnyPredictionType,
        target: thelper.typedefs.AnyTargetType,
        sample: thelper.typedefs.SampleType,
        loss: typing.Optional[float],
        iter_idx: int,
        max_iters: int,
        epoch_idx: int,
        max_epochs: int,
        output_path: typing.AnyStr,
        **kwargs,
    ) -> None:
        """Receives the latest prediction and groundtruth tensors from the training session.

        The data given here will be "consumed" internally, but it should NOT be modified. For example,
        a classification accuracy metric would accumulate the correct number of predictions in comparison
        to groundtruth labels, while a plotting logger would add new corresponding dots to a curve.

        Remember that input, prediction, and target tensors received here will all have a batch dimension!

        The exact signature of this function should match the one of the callbacks defined in
        :class:`thelper.train.base.Trainer` and specified by ``thelper.typedefs.IterCallbackParams``.
        """
        raise NotImplementedError


class ClassNamesHandler(abc.ABC):
    """Generic interface to handle class names operations for inheriting classes.

    Attributes:
        class_names: holds the list of class label names.
        class_indices: holds a mapping (dict) of class-names-to-label-indices.
    """

    # args and kwargs are for additional inputs that could be passed down involuntarily, but that are not necessary
    def __init__(
        self,
        class_names: typing.Optional[thelper.typedefs.LabelMapping] = None,
        *args,
        **kwargs
    ) -> None:
        """Initializes the class names array, if an object is provided."""
        self.class_names = class_names

    @property
    def class_names(self) -> typing.List[str]:
        """Returns the list of class names considered "of interest" by the derived class."""
        return self._class_names

    @class_names.setter
    def class_names(self, class_names: typing.Union[typing.AnyStr, typing.Iterable[typing.AnyStr]]) -> None:
        """Sets the list of class names considered "of interest" by the derived class."""
        if class_names is None:
            self._class_names = None
            self._class_indices = None
            return
        if isinstance(class_names, str) and os.path.exists(class_names):
            class_names = thelper.utils.load_config(class_names, add_name_if_missing=False)
        if isinstance(class_names, dict):
            indices_as_keys = [idx in class_names or str(idx) in class_names
                               for idx in range(len(class_names))]
            indices_as_values = [idx in class_names.values() or str(idx) in class_names.values()
                                 for idx in range(len(class_names))]
            missing_indices = {idx: not (a or b) for idx, a, b in
                               zip(range(len(class_names)), indices_as_keys, indices_as_values)}
            assert not any(missing_indices.values()), \
                f"labeling is not contiguous, missing indices:\n\t{[k for k, v in missing_indices.items() if v]}"
            assert all(indices_as_keys) or all(indices_as_values), "cannot mix indices in keys and values"
            if all(indices_as_keys):
                class_names = [thelper.utils.get_key([idx, str(idx)], class_names)
                               for idx in range(len(class_names))]
            elif all(indices_as_values):
                class_names = [k for idx in range(len(class_names))
                               for k, v in class_names.items() if v == idx or v == str(idx)]
        if isinstance(class_names, np.ndarray):
            assert class_names.ndim == 1, "class names array should be 1-dimensional"
            class_names = class_names.tolist()
        assert isinstance(class_names, list), "expected class names to be provided as an array"
        assert all([isinstance(name, str) for name in class_names]), "all classes must be named with strings"
        assert len(class_names) >= 1, "should have at least one class!"
        if len(class_names) != len(set(class_names)):
            # no longer throwing here, imagenet possesses such a case ('crane#134' and 'crane#517')
            logger.warning("found duplicated name(s) in class list, might be a data entry problem...")
            dupes = {name: count for name, count in collections.Counter(class_names).items() if count > 1}
            logger.debug(f"duplicated classes:\n{pprint.pformat(dupes, indent=2)}")
            class_names = [name if class_names.count(name) == 1 else name + "#" + str(idx)
                           for idx, name in enumerate(class_names)]
        self._class_names = copy.deepcopy(class_names)
        self._class_indices = {class_name: idx for idx, class_name in enumerate(class_names)}

    @property
    def class_indices(self) -> thelper.typedefs.ReversedLabelMapping:
        """Returns the class-name-to-index map used for encoding labels as integers."""
        return self._class_indices

    @class_indices.setter
    def class_indices(self, class_indices: thelper.typedefs.ReversedLabelMapping) -> None:
        """Sets the class-name-to-index map used for encoding labels as integers."""
        assert class_indices is None or isinstance(class_indices, dict), "indices must be provided as dictionary"
        self.class_names = class_indices


class FormatHandler(abc.ABC):
    """Generic interface to handle format output operations for inheriting classes.

    If :attr:`format` is specified and matches a supported one (with a matching ``report_<format>`` method), this
    method is used to generate the output. Defaults to ``"text"`` if not specified or provided value is not found
    within supported formatting methods.

    Attributes:
        format: format to be used for producing the report (default: "text")
        ext: extension associated with generated format (default: "txt")
    """

    # corresponding formats should have preferred extension last
    # extension -> format
    __formats__ = {
        "text": "text",
        "txt": "text",
        "csv": "csv",
        "yaml": "yaml",
        "yml": "yaml",
        "json": "json",
        "geojson": "geojson",
    }
    # format -> extension
    __fmt_ext__ = {fmt: ext for ext, fmt in __formats__.items()}

    # args and kwargs are for additional inputs that could be passed down involuntarily, but that are not necessary
    def __init__(
        self,
        format: typing.Optional[typing.AnyStr] = "text",
        *args,
        **kwargs
    ) -> None:
        self.format = None
        self.ext = None
        self.solve_format(format)

    def solve_format(
        self,
        format: typing.Optional[typing.AnyStr] = None,
    ) -> None:
        self.format = self.__formats__.get(format, "text")
        self.ext = self.__fmt_ext__.get(self.format, "txt")

    def report(
        self,
        format: typing.Optional[typing.AnyStr] = None,
    ) -> typing.Optional[typing.AnyStr]:
        """
        Returns the report as a print-friendly string, matching the specified format if specified in configuration.

        Args:
            format: format to be used for producing the report (default: initialization attribute or "text" if invalid)
        """
        self.solve_format(format or self.format or "text")
        if isinstance(self.format, str):
            formatter = getattr(self, f"report_{self.format.lower()}", None)
            if formatter is not None:
                return formatter()
        return self.report_text()

    @abc.abstractmethod
    def report_text(self) -> typing.Optional[typing.AnyStr]:
        """Must be implemented by inheriting classes. Default report text representation."""
        raise NotImplementedError


class ColorMapHandler(abc.ABC):
    """Generic interface to handle color map output operations for inheriting classes.

    If :attr:`color_map` is specified, validation ensure that the obtained map is converted from the various typing
    combinations. If :attr:`dontcare` index is provided, the corresponding labels (pixels) matching that value will be
    ignored and displayed by default as black, unless corresponding index/color is already within the color map.

    Can be combined with :class:`ClassNamesHandler` to take advantage of class indices extracted from its name mapping.

    The result is a completely defined index to (R,G,B) mapping of labels.

    Attributes:
        color_map: index to single or 3-int pairs to employ for coloring outputs.
        dontcare: label index to mark as 'ignore' candidate (for coloring but also possibly for metrics)
        background: alias to dontcare, for different naming convention across task concepts.
    """

    _ignore_names = ["dontcare", "background"]

    # args and kwargs are for additional inputs that could be passed down involuntarily, but that are not necessary
    def __init__(
        self,
        color_map: typing.Optional[thelper.typedefs.ClassColorMap] = None,
        dontcare: typing.Optional[thelper.typedefs.LabelIndex] = None,
        background: typing.Optional[thelper.typedefs.LabelIndex] = None,
        *args,
        **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        if not hasattr(self, "class_indices") and color_map:
            self.class_indices = {
                str(key): i if not (str.isnumeric(key) or isinstance(key, int)) else int(key)
                for i, key in enumerate(color_map)  # mapping can have label index or name based keys
            }
        assert dontcare is None or background is None, "only one 'ignore' keyword can be provided"
        self.ignore = dontcare if background is None else background
        self.color_map = color_map

    @property
    def color_map(self) -> typing.Optional[thelper.typedefs.ClassColorMap]:
        """Returns the color map used to swap label indices for colors when displaying results."""
        return self._color_map

    @color_map.setter
    def color_map(self, color_map: typing.Optional[thelper.typedefs.ClassColorMap]) -> None:
        """Sets the color map used to swap label indices for colors when displaying results."""
        if color_map is not None:
            assert isinstance(color_map, dict), "color map should be given as dictionary"
            self._color_map = {}
            assert all([isinstance(k, int) for k in color_map]) or all([isinstance(k, str) for k in color_map]), \
                "color map keys should be only class names or only class indices"

            for key, val in color_map.items():
                if isinstance(key, str):
                    if key in self._ignore_names and self.ignore is not None:
                        key = self.ignore
                    else:
                        assert key in self.class_indices, f"could not find color map key '{key}' in class names"
                        key = self.class_indices[key]
                assert key in self.class_indices.values() or key == self.ignore, f"unrecognized class index '{key}'"
                if isinstance(val, int):
                    val = (val, val, val)
                if isinstance(val, (list, tuple)):
                    val = np.asarray(val)
                assert isinstance(val, np.ndarray) and val.size == 3, \
                    "color values should be given as triplets of integers or unique integers"
                self._color_map[key] = val
            if self.ignore is not None and self.ignore not in self._color_map and self._color_map:
                self._color_map[self.ignore] = np.asarray([0, 0, 0])  # use black as default 'ignore' color
        else:
            self._color_map = {}

    @property
    def ignore(self) -> typing.Optional[thelper.typedefs.LabelIndex]:
        """Returns 'ignore' label value used in loss functions (can be ``None``)."""
        return self._ignore

    @ignore.setter
    def ignore(self, ignore: typing.Optional[thelper.typedefs.LabelIndex]) -> None:
        """Sets the 'ignore' label value for this task (can be ``None``)."""
        if ignore is not None:
            assert isinstance(ignore, int), "'ignore' value should be integer (index)"
            if self.class_indices:
                ignore_name = None
                for _ignore in self._ignore_names:
                    class_ignore = None if _ignore not in self.class_indices else self.class_indices[_ignore]
                    if class_ignore is not None:
                        ignore_name = _ignore
                        ignore = class_ignore
                        break
                assert ignore not in self.class_indices.values() or \
                       (ignore_name and self.class_indices[ignore_name] == ignore), \
                       "found '{}' value '{}' tied to another class label".format(ignore_name, ignore)
        self._ignore = ignore

    # aliases for other classes
    background = property(ignore.fget, ignore.fset)
    dontcare = property(ignore.fget, ignore.fset)
