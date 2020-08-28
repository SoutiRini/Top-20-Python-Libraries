from __future__ import absolute_import, division, print_function, unicode_literals

import copy
import itertools
import warnings

import torch
import torch.nn as nn
import torch.nn.intrinsic as nni
import torch.nn.quantized as nnq

from .default_mappings import (DEFAULT_DYNAMIC_MODULE_MAPPING,
                               DEFAULT_MODULE_MAPPING,
                               DEFAULT_QAT_MODULE_MAPPING,
                               DEFAULT_QCONFIG_PROPAGATE_WHITE_LIST)
from .stubs import DeQuantStub, QuantWrapper
from .qconfig import default_dynamic_qconfig, float16_dynamic_qconfig

def _propagate_qconfig_helper(module, qconfig_dict, white_list=None,
                              qconfig_parent=None, prefix=''):
    r"""This is a helper function for `propagate_qconfig_`

    Args:
        module: input module
        qconfig_dict: dictionary that maps from name of submodule to quantization
                     configuration
        white_list: list of quantizable modules
        qconfig_parent: quantization config of parent module, we will fallback to
                       this config when there is no specified config for current
                       module
        prefix: corresponding prefix of the current module, used as key in
                qconfig_dict

    Return:
        None, module is modified inplace with qconfig attached
    """
    # TODO: Add test
    if white_list is None:
        white_list = DEFAULT_QCONFIG_PROPAGATE_WHITE_LIST

    module_qconfig = qconfig_dict.get(type(module), qconfig_parent)
    module_qconfig = qconfig_dict.get(prefix, module_qconfig)
    module_qconfig = getattr(module, 'qconfig', module_qconfig)

    if type(module) in white_list:
        module.qconfig = module_qconfig
    for name, child in module.named_children():
        module_prefix = prefix + '.' + name if prefix else name
        _propagate_qconfig_helper(child, qconfig_dict, white_list,
                                  module_qconfig, module_prefix)

# TODO(jerryzh): expose white_list
def propagate_qconfig_(module, qconfig_dict=None, white_list=None):
    r"""Propagate qconfig through the module hierarchy and assign `qconfig`
    attribute on each leaf module

    Args:
        module: input module
        qconfig_dict: dictionary that maps from name or type of submodule to
            quantization configuration, qconfig applies to all submodules of a
            given module unless qconfig for the submodules are specified (when
            the submodule already has qconfig attribute)

    Return:
        None, module is modified inplace with qconfig attached
    """
    if qconfig_dict is None:
        qconfig_dict = {}
    _propagate_qconfig_helper(module, qconfig_dict, white_list)

def _observer_forward_hook(self, input, output):
    r"""Forward hook that calls observer on the output
    """
    return self.activation_post_process(output)

def add_observer_(module, non_leaf_module_list=None, device=None):
    r"""Add observer for the leaf child of the module.

    This function insert observer module to all leaf child module that
    has a valid qconfig attribute.

    Args:
        module: input module with qconfig attributes for all the leaf modules that we want to quantize
        device: parent device, if any
        non_leaf_module_list: list of non-leaf modules we want to add observer

    Return:
        None, module is modified inplace with added observer modules and forward_hooks
    """
    # respect device affinity when adding observers
    if device is None:
        devices = get_unique_devices_(module)
        assert len(devices) <= 1, (
            "add_observer_ only works with cpu or single-device CUDA modules, "
            "but got devices {}".format(devices)
        )
        device = next(iter(devices)) if len(devices) > 0 else None

    for child in module.children():
        if type(child) == nnq.FloatFunctional or type(child) == nnq.QFunctional:
            if hasattr(child, 'qconfig') and child.qconfig is not None:
                activation = child.qconfig.activation()
                if device is not None:
                    activation.to(device)
                child.activation_post_process = activation
        elif non_leaf_module_list is not None and type(child) in non_leaf_module_list:
            if hasattr(child, 'qconfig') and child.qconfig is not None:
                child.add_module('activation_post_process', child.qconfig.activation())
                child.register_forward_hook(_observer_forward_hook)
        else:
            add_observer_(child, non_leaf_module_list, device)

    # Insert observers only for leaf nodes, note that this observer is for
    # the output of the module, for input QuantStub will observe them
    if hasattr(module, 'qconfig') and module.qconfig is not None and \
       len(module._modules) == 0 and not isinstance(module, torch.nn.Sequential):
        # observer and hook will be gone after we swap the module
        activation = module.qconfig.activation()
        if device is not None:
            activation.to(device)
        module.add_module('activation_post_process', activation)
        # Register observer as the first entry in the hook list
        # All post forward hooks are preserved and will be executed after the observer before convert
        handle = module.register_forward_hook(_observer_forward_hook)
        module._forward_hooks.move_to_end(handle.id, last=False)

def get_unique_devices_(module):
    return {p.device for p in module.parameters()} | \
        {p.device for p in module.buffers()}

def add_quant_dequant(module):
    r"""Wrap the leaf child module in QuantWrapper if it has a valid qconfig
    Note that this function will modify the children of module inplace and it
    can return a new module which wraps the input module as well.

    Args:
        module: input module with qconfig attributes for all the leaf modules
        that we want to quantize

    Return:
        Either the inplace modified module with submodules wrapped in
        `QuantWrapper` based on qconfig or a new `QuantWrapper` module which
        wraps the input module, the latter case only happens when the input
        module is a leaf module and we want to quantize it.
    """
    if len(module._modules) == 0 and hasattr(module, 'qconfig') and module.qconfig:
        return QuantWrapper(module)

    for name, child in module.named_children():
        module._modules[name] = add_quant_dequant(child)
    return module

def prepare(model, inplace=False, white_list=DEFAULT_QCONFIG_PROPAGATE_WHITE_LIST, observer_non_leaf_module_list=None):
    r"""Prepares a copy of the model for quantization calibration or quantization-aware training.

    Quantization configuration should be assigned preemptively
    to individual submodules in `.qconfig` attribute.

    The model will be attached with observer or fake quant modules, and qconfig
    will be propagated.

    Args:
        model: input model to be modified in-place
        inplace: carry out model transformations in-place, the original module is mutated
        white_list: list of quantizable modules
        observer_non_leaf_module_list: list of non-leaf modules we want to add observer
    """
    if not inplace:
        model = copy.deepcopy(model)
    propagate_qconfig_(model, qconfig_dict=None, white_list=white_list)
    # sanity check common API misusage
    if not any(hasattr(m, 'qconfig') and m.qconfig for m in model.modules()):
        warnings.warn("None of the submodule got qconfig applied. Make sure you "
                      "passed correct configuration through `qconfig_dict` or "
                      "by assigning the `.qconfig` attribute directly on submodules")
    add_observer_(model, observer_non_leaf_module_list)
    return model

def _remove_qconfig(module):
    r"""Clean up the qconfig left in the module so that new qconfig can be
    propagated.

    Args:
        module: module to be cleaned up
    """
    for child in module.children():
        _remove_qconfig(child)

    if hasattr(module, "qconfig"):
        del module.qconfig

def quantize(model, run_fn, run_args, mapping=None, inplace=False):
    r"""Quantize the input float model with post training static quantization.

    First it will prepare the model for calibration, then it calls
    `run_fn` which will run the calibration step, after that we will
    convert the model to a quantized model.

    Args:
        model: input float model
        run_fn: a calibration function for calibrating the prepared model
        run_args: positional arguments for `run_fn`
        inplace: carry out model transformations in-place, the original module is mutated
        mapping: correspondence between original module types and quantized counterparts

    Return:
        Quantized model.
    """
    if mapping is None:
        mapping = DEFAULT_MODULE_MAPPING
    if not inplace:
        model = copy.deepcopy(model)
    model.eval()
    prepare(model, inplace=True)
    run_fn(model, run_args)
    convert(model, mapping, inplace=True)
    _remove_qconfig(model)
    return model

def quantize_dynamic(model, qconfig_spec=None, dtype=torch.qint8,
                     mapping=None, inplace=False):
    r"""Converts a float model to dynamic (i.e. weights-only) quantized model.

    Replaces specified modules with dynamic weight-only quantized versions and output the quantized model.

    For simplest usage provide `dtype` argument that can be float16 or qint8. Weight-only quantization
    by default is performed for layers with large weights size - i.e. Linear and RNN variants.

    Fine grained control is possible with `qconfig` and `mapping` that act similarly to `quantize()`.
    If `qconfig` is provided, the `dtype` argument is ignored.

    Args:
        module: input model
        qconfig_spec: Either:

            - A dictionary that maps from name or type of submodule to quantization
              configuration, qconfig applies to all submodules of a given
              module unless qconfig for the submodules are specified (when the
              submodule already has qconfig attribute). Entries in the dictionary
              need to be QConfigDynamic instances.

            - A set of types and/or submodule names to apply dynamic quantization to,
              in which case the `dtype` argument is used to specify the bit-width

        inplace: carry out model transformations in-place, the original module is mutated
        mapping: maps type of a submodule to a type of corresponding dynamically quantized version
            with which the submodule needs to be replaced

    """
    if qconfig_spec is None:
        if dtype == torch.qint8:
            qconfig_spec = {
                nn.Linear : default_dynamic_qconfig,
                nn.LSTM : default_dynamic_qconfig,
                nn.GRU : default_dynamic_qconfig,
                nn.LSTMCell : default_dynamic_qconfig,
                nn.RNNCell : default_dynamic_qconfig,
                nn.GRUCell : default_dynamic_qconfig,
            }
        elif dtype == torch.float16:
            qconfig_spec = {
                nn.Linear : float16_dynamic_qconfig,
                nn.LSTM : float16_dynamic_qconfig,
                nn.GRU : float16_dynamic_qconfig,
                nn.LSTMCell : float16_dynamic_qconfig,
                nn.RNNCell : float16_dynamic_qconfig,
                nn.GRUCell : float16_dynamic_qconfig,
            }
        else:
            raise ValueError(
                "Don't know how to quantize with default settings for {}. Provide full qconfig please".format(dtype))
    elif isinstance(qconfig_spec, set):
        if dtype is torch.qint8:
            default_qconfig = default_dynamic_qconfig
        elif dtype is torch.float16:
            default_qconfig = float16_dynamic_qconfig
        else:
            raise RuntimeError('Unknown dtype specified for quantize_dynamic: ', str(dtype))
        qconfig_spec = dict(zip(qconfig_spec, itertools.repeat(default_qconfig)))

    if mapping is None:
        mapping = DEFAULT_DYNAMIC_MODULE_MAPPING

    if not inplace:
        model = copy.deepcopy(model)
    model.eval()
    propagate_qconfig_(model, qconfig_spec)
    convert(model, mapping, inplace=True)
    _remove_qconfig(model)
    return model

def prepare_qat(model, mapping=None, inplace=False):
    r"""
    Prepares a copy of the model for quantization calibration or
    quantization-aware training and converts it to quantized version.

    Quantization configuration should be assigned preemptively
    to individual submodules in `.qconfig` attribute.

    Args:
        model: input model to be modified in-place
        mapping: dictionary that maps float modules to quantized modules to be
                 replaced.
        inplace: carry out model transformations in-place, the original module
                 is mutated
    """
    if mapping is None:
        mapping = DEFAULT_QAT_MODULE_MAPPING
    model = prepare(model, inplace=inplace)
    convert(model, mapping, inplace=True)
    return model

def quantize_qat(model, run_fn, run_args, inplace=False):
    r"""Do quantization aware training and output a quantized model

    Args:
        model: input model
        run_fn: a function for evaluating the prepared model, can be a
                function that simply runs the prepared model or a training
                loop
        run_args: positional arguments for `run_fn`

    Return:
        Quantized model.
    """
    if not inplace:
        model = copy.deepcopy(model)
    model.train()
    prepare_qat(model, inplace=True)
    run_fn(model, run_args)
    convert(model, inplace=True)
    return model

def convert(module, mapping=None, inplace=False):
    r"""Converts the float module with observers (where we can get quantization
    parameters) to a quantized module.

    Args:
        module: calibrated module with observers
        mapping: a dictionary that maps from float module type to quantized
                 module type, can be overwritten to allow swapping user defined
                 Modules
        inplace: carry out model transformations in-place, the original module
                 is mutated

    """
    if mapping is None:
        mapping = DEFAULT_MODULE_MAPPING
    if not inplace:
        module = copy.deepcopy(module)
    reassign = {}
    # TODO(jerryzh): remove after deciding on the impl of intrinsic modules
    # This is required because intrinsic modules right now are implemented as
    # nn.Sequential and we don't want to swap their constituents
    SWAPPABLE_MODULES = (nni.ConvBn2d,
                         nni.ConvBnReLU2d,
                         nni.LinearReLU,
                         nni.BNReLU2d,
                         nni.BNReLU3d,
                         nni.ConvBn1d,
                         nni.ConvReLU1d,
                         nni.ConvBnReLU1d,
                         nni.ConvReLU2d,
                         nni.ConvReLU3d)

    for name, mod in module.named_children():
        if type(mod) not in SWAPPABLE_MODULES:
            convert(mod, mapping, inplace=True)
        reassign[name] = swap_module(mod, mapping)

    for key, value in reassign.items():
        module._modules[key] = value

    return module

def swap_module(mod, mapping):
    r"""Swaps the module if it has a quantized counterpart and it has an
    `observer` attached.

    Args:
        mod: input module
        mapping: a dictionary that maps from nn module to nnq module

    Return:
        The corresponding quantized module of `mod`
    """
    new_mod = mod
    # Always replace dequantstub with dequantize
    if hasattr(mod, 'qconfig') and mod.qconfig is not None or type(mod) == DeQuantStub:
        if type(mod) in mapping:
            # respect device affinity when swapping modules
            devices = get_unique_devices_(mod)
            assert len(devices) <= 1, (
                "swap_module only works with cpu or single-device CUDA modules, "
                "but got devices {}".format(devices)
            )
            device = next(iter(devices)) if len(devices) > 0 else None
            new_mod = mapping[type(mod)].from_float(mod)
            # Preserve module's pre forward hooks. They'll be called on quantized input
            for pre_hook_fn in mod._forward_pre_hooks.values():
                new_mod.register_forward_pre_hook(pre_hook_fn)
            # Preserve module's post forward hooks except _observer_forward_hook
            # After convert they'll work with quantized output
            for hook_fn in mod._forward_hooks.values():
                if hook_fn is not _observer_forward_hook:
                    new_mod.register_forward_hook(hook_fn)
            if device:
                new_mod.to(device)
    return new_mod

def get_observer_dict(mod, target_dict, prefix=""):
    r"""Traverse the modules and save all observers into dict.
    This is mainly used for quantization accuracy debug
    Args:
        mod: the top module we want to save all observers
        prefix: the prefix for the current module
        target_dict: the dictionary used to save all the observers
    """
    def get_prefix(prefix):
        return prefix if prefix == "" else prefix + '.'

    if hasattr(mod, 'activation_post_process'):
        target_dict[get_prefix(prefix) + 'activation_post_process'] = mod.activation_post_process
    for name, child in mod.named_children():
        module_prefix = get_prefix(prefix) + name if prefix else name
        get_observer_dict(child, target_dict, module_prefix)
