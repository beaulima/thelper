import logging
import os
from abc import ABC
from abc import abstractmethod

import numpy as np
import torch
import torch.nn

import thelper
import thelper.modules
import thelper.tasks
import thelper.utils

logger = logging.getLogger(__name__)


def load_model(config, task, save_dir=None, ckptdata=None):
    """Instantiates a model based on a provided task object.

    The configuration must be given as a dictionary object. This dictionary will be parsed for a 'model' field.
    This field is expected to be a dictionary itself. It may then specify a type to instantiate as well as the
    parameters to provide to that class constructor, or a path to a checkpoint from which a model should be loaded.

    All models must derive from :class:`thelper.modules.Module`, or they must be instantiable through
    :class:`thelper.modules.ExternalModule` (or one of its specialized classes). The provided task object will
    be used to make sure that the model has the required input/output layers for the requested objective.

    If checkpoint data is provided by the caller, the weights it contains will be loaded into the returned model.

    Usage examples inside a session configuration file::

        # ...
        # the function will look for a 'model' field in the provided config dict
        "model": {
            # the type provides the class name to instantiate an object from
            "type": "thelper.modules.mobilenet.MobileNetV2",
            # the parameters listed below are passed to the model's constructor
            "params": [
                # ...
            ]
        # ...

    Args:
        config: a session dictionary that provides a 'model' field containing a dictionary.
        task: a task object that will be passed to the model's constructor in order to specialize it.
        save_dir: if not ``None``, a log file containing model information will be created there.
        ckptdata: raw checkpoint data loaded via ``torch.load()``; the model will be given its previous state.

    Returns:
        The instantiated model, compatible with the interface of both :class:`thelper.modules.Module`
        and ``torch.nn.Module``.

    .. seealso::
        :class:`thelper.modules.Module`
        :class:`thelper.modules.ExternalModule`
        :class:`thelper.tasks.Task`
    """
    if not isinstance(task, thelper.tasks.Task):
        raise AssertionError("bad task type passed to load_model")
    if save_dir is not None:
        modules_logger_path = os.path.join(save_dir, "logs", "modules.log")
        modules_logger_format = logging.Formatter("[%(asctime)s - %(process)s] %(levelname)s : %(message)s")
        modules_logger_fh = logging.FileHandler(modules_logger_path)
        modules_logger_fh.setFormatter(modules_logger_format)
        logger.addHandler(modules_logger_fh)
        logger.info("created modules log for session '%s'" % config["name"])
    logger.debug("loading model")
    if "model" not in config or not config["model"]:
        raise AssertionError("config missing 'model' field")
    model_config = config["model"]
    if "ckptdata" in model_config:
        if ckptdata is not None:
            logger.warning("config asked to reload ckpt from path, but ckpt already loaded from elsewhere")
        else:
            logger.debug("model config asked for an older model to be loaded through a checkpoint")
            if not isinstance(model_config["ckptdata"], str):
                raise AssertionError("unexpected model config ckptdata field type (should be path)")
            map_location = thelper.utils.get_key_def("map_location", model_config, "cpu")
            ckptdata = thelper.utils.load_checkpoint(model_config["ckptdata"], map_location=map_location)
        if "type" in model_config or "params" in model_config:
            logger.warning("should not provide 'type' or 'params' fields in model config if loading a checkpoint")
    new_task, model, model_type, model_params, model_state = None, None, None, None, None
    if ckptdata is not None:
        # if checkpoint available, instantiate old model, load weights, and reconfigure for new task
        if "name" not in ckptdata or not isinstance(ckptdata["name"], str):
            raise AssertionError("invalid checkpoint, cannot reload previous session name")
        new_task = task  # the 'new task' will later be applied to specialize the model, once it is loaded
        if "model" not in ckptdata or not isinstance(ckptdata["model"], (Module, dict)):
            raise AssertionError("invalid checkpoint, cannot reload previous model state")
        if isinstance(ckptdata["model"], Module):
            logger.debug("loading model object directly from session '%s'" % ckptdata["name"])
            model = ckptdata["model"]
            if model.get_name() != ckptdata["model_type"]:
                raise AssertionError("old model type mistmatch with ckptdata type")
        elif isinstance(ckptdata["model"], dict):
            logger.debug("loading model type/params from session '%s'" % ckptdata["name"])
            model_state = ckptdata["model"]
            if "task" not in ckptdata or not isinstance(ckptdata["task"], (thelper.tasks.Task, str)):
                raise AssertionError("invalid checkpoint, cannot reload previous model task")
            task = thelper.tasks.load_task(ckptdata["task"]) if isinstance(ckptdata["task"], str) else ckptdata["task"]
            if "model_type" not in ckptdata or not isinstance(ckptdata["model_type"], str):
                raise AssertionError("invalid checkpoint, cannot reload previous model type")
            model_type = thelper.utils.import_class(ckptdata["model_type"])
            if "model_params" not in ckptdata or not isinstance(ckptdata["model_params"], dict):
                raise AssertionError("invalid checkpoint, cannot reload previous model params")
            model_params = ckptdata["model_params"]
            if "config" not in ckptdata or not isinstance(ckptdata["config"], dict):
                raise AssertionError("invalid checkpoint, cannot reload previous session config")
            old_config = ckptdata["config"]
            if "model" not in old_config or not isinstance(old_config["model"], dict):
                raise AssertionError("invalid checkpoint, cannot reload previous model config")
            old_model_config = old_config["model"]
            if "type" in old_model_config and thelper.utils.import_class(old_model_config["type"]) != model_type:
                raise AssertionError("old model config 'type' field mistmatch with ckptdata type")
    else:
        logger.debug("loading model type/params current config")
        if "type" not in model_config or not model_config["type"]:
            raise AssertionError("model config missing 'type' field")
        model_type = thelper.utils.import_class(model_config["type"])
        model_params = thelper.utils.get_key_def("params", model_config, {})
    if model is None:
        # if model not already loaded from checkpoint, instantiate it fully from type/params/task
        if model_type is None or model_params is None:
            raise AssertionError("messed up logic above")
        logger.debug("model_type = %s" % str(model_type))
        logger.debug("model_params = %s" % str(model_params))
        logger.debug("task = %s" % str(task))
        if issubclass(model_type, Module):
            model = model_type(task=task, **model_params)
        else:
            if type(task) == thelper.tasks.Classification:
                model = ExternalClassifModule(model_type, task=task, config=model_params)
            else:
                model = ExternalModule(model_type, task=task, config=model_params)
        if model_state is not None:
            logger.debug("loading state dictionary from checkpoint into model")
            model.load_state_dict(model_state)
    if new_task is not None:
        logger.debug("specializing model for task = %s" % str(new_task))
        model.set_task(new_task)
    if model.config is None:
        model.config = model_params
    if hasattr(model, "summary"):
        model.summary()
    return model


class Module(torch.nn.Module, ABC):
    """Model inteface used to hold a task object.

    This interface is built on top of ``torch.nn.Module`` and should remain fully compatible with it.

    All models used in the framework should derive from this interface, and therefore expect a task object as
    the first argument of their constructor. Their implementation may decide to ignore this task object when
    building their internal layers, but using it should help specialize the network by specifying e.g. the
    number of classes to support.

    .. seealso::
        :func:`thelper.modules.load_model`
        :class:`thelper.tasks.Task`
    """

    def __init__(self, task, config=None):
        """Receives a task object to hold internally for model specialization."""
        super().__init__()
        if task is None or not isinstance(task, thelper.tasks.Task):
            raise AssertionError("task must derive from thelper.tasks.Task")
        self.task = task
        self.config = config

    @abstractmethod
    def forward(self, *input):
        """Transforms an input tensor in order to generate a prediction."""
        raise NotImplementedError

    @abstractmethod
    def set_task(self, task):
        """Adapts the model to support a new task, replacing layers if needed."""
        raise NotImplementedError

    def summary(self):
        """Prints a summary of the model using the ``thelper.modules`` logger."""
        params = filter(lambda p: p.requires_grad, self.parameters())
        count = sum([np.prod(p.size()) for p in params])
        logger.info("module '%s' parameter count: %d" % (self.get_name(), count))
        logger.info(self)

    def get_name(self):
        """Returns the name of this module (by default, its fully qualified class name)."""
        return self.__class__.__module__ + "." + self.__class__.__qualname__


class ExternalModule(Module):
    """Model inteface used to hold a task object for an external implementation.

    This interface is built on top of ``torch.nn.Module`` and should remain fully compatible with it. It is
    automatically used when instantiating a model via :func:`thelper.modules.load_model` that is not derived
    from :class:`thelper.modules.Module`. Its only purpose is to hold the task object, and redirect
    :func:`thelper.modules.Module.forward` to the actual model's transformation function. It can also be
    specialized to automatically adapt some external models after their construction using the knowledge
    contained in the task object.

    .. seealso::
        :class:`thelper.modules.Module`
        :class:`thelper.modules.ExternalClassifModule`
        :func:`thelper.modules.load_model`
        :class:`thelper.tasks.Task`
    """

    def __init__(self, model_type, task, config=None):
        """Receives a task object to hold internally for model specialization."""
        super().__init__(task=task, config=config)
        logger.info("instantiating external module '%s'..." % str(model_type))
        self.model_type = model_type
        self.model = model_type(**config)
        if not hasattr(self.model, "forward"):
            raise AssertionError("external module must implement 'forward' method")

    def load_state_dict(self, state_dict, strict=True):
        """Loads the state dict of an external model."""
        self.model.load_state_dict(state_dict=state_dict, strict=strict)

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        """Returns the state dict of the external model."""
        return self.model.state_dict(destination=destination, prefix=prefix, keep_vars=keep_vars)

    def forward(self, *input):
        """Transforms an input tensor in order to generate a prediction."""
        return self.model(*input)

    def set_task(self, task):
        """Stores the new task internally.

        Note that since this external module handler is generic, it does not know what to do with the task,
        so it just assumes that the model is already set up. Specialized external module handlers will instead
        attempt to modify the model they wrap.
        """
        if task is None or not isinstance(task, thelper.tasks.Task):
            raise AssertionError("task must derive from thelper.tasks.Task")
        self.task = task

    def summary(self):
        """Prints a summary of the model using the ``thelper.modules`` logger."""
        params = filter(lambda p: p.requires_grad, self.model.parameters())
        count = sum([np.prod(p.size()) for p in params])
        logger.info("module '%s' parameter count: %d" % (self.get_name(), count))
        logger.info(self.model)

    def get_name(self):
        """Returns the name of this module (by default, the fully qualified class name of the external model)."""
        return self.model_type.__module__ + "." + self.model_type.__qualname__


class ExternalClassifModule(ExternalModule):
    """External model interface specialization for classification tasks.

    This interface will try to 'rewire' the last fully connected layer of the models it instantiates to match
    the number of classes to predict defined in the task object.

    .. seealso::
        :class:`thelper.modules.Module`
        :class:`thelper.modules.ExternalModule`
        :func:`thelper.modules.load_model`
        :class:`thelper.tasks.Task`
    """

    def __init__(self, model_type, task, config=None):
        """Receives a task object to hold internally for model specialization, and tries to rewire the last 'fc' layer."""
        super().__init__(model_type, task, config=config)
        self.nb_classes = None
        self.set_task(task)

    def set_task(self, task):
        """Rewires the last fully connected layer of the wrapped network to fit the given number of classification targets."""
        if type(task) != thelper.tasks.Classification:
            raise AssertionError("task passed to ExternalClassifModule should be 'thelper.tasks.Classification'")
        self.nb_classes = self.task.get_nb_classes()
        if hasattr(self.model, "fc") and isinstance(self.model.fc, torch.nn.Linear):
            if self.model.fc.out_features != self.nb_classes:
                logger.info("reconnecting fc layer for outputting %d classes..." % self.nb_classes)
                nb_features = self.model.fc.in_features
                self.model.fc = torch.nn.Linear(nb_features, self.nb_classes)
        elif hasattr(self.model, "classifier") and isinstance(self.model.classifier, torch.nn.Linear):
            if self.model.classifier.out_features != self.nb_classes:
                logger.info("reconnecting classifier layer for outputting %d classes..." % self.nb_classes)
                nb_features = self.model.classifier.in_features
                self.model.classifier = torch.nn.Linear(nb_features, self.nb_classes)
        else:
            raise AssertionError("could not reconnect fully connected layer for new classes")