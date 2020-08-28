# HEY! Trying to understand what this file does?  Read
# "what has to be done to add a Operation ..." first!

import re
import copy
from code_template import CodeTemplate


from typing import Any, Dict, List, Optional, Set, Tuple, NamedTuple

try:
    from mypy_extensions import TypedDict
except ImportError:
    # Avoid the dependency on the mypy_extensions package.
    # It is required, however, for type checking.
    def TypedDict(name, attrs, total=True):  # type: ignore
        return Dict[Any, Any]

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# what has to be done to add a Operation ...
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

# TH functions are generated into at::legacy::cpu and at::legacy::cuda,
# where they can be called directly by a native function, they can be wrapped
# by a native function that handles dispatch

LEGACY_TH_DECLARATION = CodeTemplate("""\
${return_type} ${api_name}(${formals});
""")

LEGACY_TH_DEFINITION = CodeTemplate("""\
${return_type} ${api_name}(${formals}) {
    ${device_guard_declaration}
    ${type_definition_body}
}
""")

LEGACY_TH_DEFINITION_SWITCH_STATEMENT = CodeTemplate("""\
${dispatch_scalar_type_declaration}
${switch_prologue}
switch (dispatch_scalar_type) {
    ${cases}
    default:
        AT_ERROR("${api_name} not supported on ${Type} for ", dispatch_scalar_type);
}
${switch_epilogue}
""")

LEGACY_TH_DEFINITION_CASE = CodeTemplate("""\
case ScalarType::${ScalarName}: {
    ${case_body}
    break;
}
""")

# Native functions are generated and registered on the dispatcher. We register the
# function on Backend::Undefined if it does not have backend dependent dispatch.
# In this case, it will be called for all backends, but can be overwritten on a
# per backend basis.
NATIVE_DISPATCH_DECLARATION = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals});
""")

NATIVE_DISPATCH_DEFINITION_DEFAULT = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) {
    ${device_guard_declaration}
    ${return_call} at::native::${native_type_method_dispatch}(${actuals});
}
""")

NATIVE_DISPATCH_DEFINITION_CPU_BACKEND = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) {
    ${return_call} at::native::${native_type_method_dispatch}(${actuals});
}
""")

NATIVE_DISPATCH_DEFINITION_GENERIC_BACKEND = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) {
    ${device_init}
    ${device_guard_declaration}
    ${return_call} at::native::${native_type_method_dispatch}(${actuals});
}
""")

# A schema registration specifies alias analysis for an operator, but doesn't
# actually provide an implementation.  Although our registration API allows you
# to specify all of this information at a function registration site, it's
# better to do it once at a schema registration so that we don't have to
# repeat ourselves everywhere else.
SCHEMA_REGISTRATION = CodeTemplate("""\
m.def("${unqual_schema_string}");
""")

# NOTE[UnboxedOnly] Many of our codegen templates currently exist twice, once
# in an _UNBOXEDONLY_ variant and once without _UNBOXEDONLY_. This is because
# ops that are `use_c10_dispatcher: full` need different c++ code than ops
# that aren't `use_c10_dispatcher: full` yet. The _UNBOXEDONLY_ variants
# are for ops that aren't `use_c10_dispatcher: full` yet and those code templates
# can be deleted once all ops are `use_c10_dispatcher: full`.
# If you update one of the templates, you likely also have to update the other.

# NB: Specifiction of the namespace is handled by the enclosing
# TORCH_LIBRARY macro invocation
# See NOTE[UnboxedOnly]
DEFAULT_UNBOXEDONLY_FUNCTION_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       torch::CppFunction::makeUnboxedOnly(&TypeDefault::${type_wrapper_name}));
""")

DEFAULT_FUNCTION_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       c10::impl::hacky_wrapper_for_legacy_signatures(TORCH_FN(TypeDefault::${type_wrapper_name})));
""")

# NB: In the ordinary, TypeDerived code generation work flow, specification
# of the backend is handled by the enclosing block, so the torch::dispatch
# invocation here is strictly unnecessary.  However, in the fbcode mobile
# only workflow using per-op registration, these registrations will get dumped
# in a TORCH_LIBRARY_FRAGMENT that does not have an ambient backend.  So
# the torch::dispatch specification here is important!  See
# Note [Redundancy in registration code is OK] for how we handle redundant info.
BACKEND_UNBOXEDONLY_FUNCTION_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       torch::dispatch(DispatchKey::${Backend},
                       torch::CppFunction::makeUnboxedOnly(&${Type}::${type_wrapper_name}))
);
""")

BACKEND_FUNCTION_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       torch::dispatch(DispatchKey::${Backend},
                       c10::impl::hacky_wrapper_for_legacy_signatures(TORCH_FN(${Type}::${type_wrapper_name})))
);
""")

# add non-virtual declaration to TensorBody.h
TENSOR_METHOD_DECLARATION = CodeTemplate("""\
${return_type} ${api_name}(${method_formals_with_defaults}) const;
""")

# add non-virtual declaration to Tensor.cpp
TENSOR_METHOD_DEFINITION = CodeTemplate("""\

// ${schema_string}
${return_type} Tensor::${api_name}(${method_formals}) const {
#ifdef USE_STATIC_DISPATCH
    ${static_dispatch_method_body}
#else
    static auto op = c10::Dispatcher::singleton()
        .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
        .typed<${tensor_method_cpp_signature}>();
    return op.call(${tensor_method_actuals});
#endif
}
""")

# add a method declaration in Functions.h
FUNCTION_DECLARATION = CodeTemplate("""\
CAFFE2_API ${return_type} ${api_name}(${formals_with_defaults});
""")

# add a method declaration in Functions.h
DEPRECATED_FUNCTION_DECLARATION = CodeTemplate("""\
C10_DEPRECATED CAFFE2_API ${return_type} ${api_name}(${formals_with_defaults});
""")

# add method definition in Functions.h
FUNCTION_DEFINITION = CodeTemplate("""\

// ${schema_string}
${return_type} ${api_name}(${formals}) {
#ifdef USE_STATIC_DISPATCH
    ${static_dispatch_function_body}
#else
    static auto op = c10::Dispatcher::singleton()
        .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
        .typed<${function_cpp_signature}>();
    return op.call(${function_actuals});
#endif
}
""")

# In order to rely on the linker to strip unused ops, it requires us to dispatch statically
# in Functions.h and TensorMethods.cpp.
#
# NB: The default body also needs to apply a variable guard, as in some
# situations what we think is a default body actually does have an
# explicit derivative, and thereby would have gotten unwrapped by
# the time you get to the implementation.
STATIC_DISPATCH_FUNCTION_DEFAULT_BODY = CodeTemplate("""\
at::AutoNonVariableTypeMode _var_guard(true);
${return_call} TypeDefault::${type_wrapper_name}(${actuals});
""")

STATIC_DISPATCH_FUNCTION_SWITCH_BODY = CodeTemplate("""\
at::AutoNonVariableTypeMode _var_guard(true);
${dispatch_key_init}
switch (dispatchKeyToBackend(${dispatch_key_var_name})) {
    ${static_dispatch_function_cases}
    default:
        AT_ERROR("${api_name} not implemented for ", at::toString(${dispatch_key_var_name}));
}
""")

STATIC_DISPATCH_FUNCTION_SWITCH_CASE = CodeTemplate("""\
case Backend::${backend}:
    ${return_call} ${backend}Type::${type_wrapper_name}(${actuals});
    break;
""")

IFDEF_BLOCK = CodeTemplate("""\
#ifdef ${ifdef_guard}
${content}
#endif
""")

# add a native declaration for a native function
NATIVE_DECLARATION = CodeTemplate("""\
CAFFE2_API ${return_type} ${native_type_method_dispatch}(${formals_with_defaults});
""")

CALL_TEMPLATE = CodeTemplate("${cname}(${actuals})")

OPERATOR_NAME = CodeTemplate("aten::${operator_name}")

OPERATOR_NAME_FULL = CodeTemplate("""\
    {"aten::${operator_name}", "${overload_name}"},
""")

# scalar_name, c_type, accreal, is_floating_type
scalar_types = [
    ('Bool', 'bool', 'BoolAccrealNotDefined', False),
    ('Byte', 'uint8_t', 'Long', False),
    ('Char', 'int8_t', 'Long', False),
    ('Double', 'double', 'Double', True),
    ('Float', 'float', 'Double', True),
    ('Int', 'int', 'Long', False),
    ('Long', 'int64_t', 'Long', False),
    ('Short', 'int16_t', 'Long', False),
    ('Half', 'Half', 'Double', True),
    ('BFloat16', 'BFloat16', 'BFloat16AccrealNotDefined', True),
    ('ComplexFloat', 'ComplexFloat', 'ComplexDouble', False),
    ('ComplexDouble', 'ComplexDouble', 'ComplexDouble', False),
]

static_dispatch_backends = ['CPU', 'QuantizedCPU', 'Vulkan']
static_dispatch_backends_ifdef_guard = {'Vulkan' : 'USE_VULKAN'}


class NYIError(Exception):
    """Indicates we don't support this declaration yet"""

    __slots__ = ['reason']

    def __init__(self, reason):
        self.reason = reason


TYPE_FORMAL_GENERIC = {
    'THTensor*': 'Tensor &',
    'THByteTensor*': 'Tensor &',
    'THIndexTensor*': 'Tensor &',
    'THBoolTensor*': 'Tensor &',
    'IntArrayRefSize': 'IntArrayRef',
    'accreal': 'Scalar',
    'real': 'Scalar',
    'long': 'int64_t',
}

DYNAMIC_TYPE = {
    'THTensor*': 'Tensor',
    'THByteTensor*': 'ByteTensor',
    'THBoolTensor*': 'BoolTensor',
    'THIndexTensor*': 'IndexTensor',
    'IntArrayRefSize': 'IntArrayRef',
    'accreal': 'accreal',
    'real': 'real',
    'long': 'int64_t',
}

NATIVE_DYNAMIC_TYPE = {
    'Tensor &': 'Tensor',
    'const Tensor &': 'Tensor',
}

TYPE_RETURN = {
    'THTensor*': 'Tensor',
    'THIndexTensor*': 'Tensor',
    'THByteTensor*': 'Tensor',
    'THBoolTensor*': 'Tensor',
    'real': 'Tensor',
    'accreal': 'Tensor',
    'long': 'int64_t',
}

CHECKED_CAST = {
    'THTensor*':
        CodeTemplate(
            'checked_dense_tensor_unwrap('
            '${arg_name}, "${arg_name}", ${arg_pos}, "${api_name}", ${null_okay}, '
            'DeviceType::${DeviceType}, ${scalar_type})'),
    'THByteTensor*':
        CodeTemplate(
            'checked_dense_tensor_unwrap('
            '${arg_name}, "${arg_name}", ${arg_pos}, "${api_name}", ${null_okay}, '
            'DeviceType::${DeviceType}, ScalarType::Byte)'),
    'THBoolTensor*':
        CodeTemplate(
            'checked_dense_tensor_unwrap('
            '${arg_name}, "${arg_name}", ${arg_pos}, "${api_name}", ${null_okay}, '
            'DeviceType::${DeviceType}, ScalarType::Bool)'),
    'THIndexTensor*':
        CodeTemplate(
            'checked_dense_tensor_unwrap('
            '${arg_name}, "${arg_name}", ${arg_pos}, "${api_name}", ${null_okay}, '
            'DeviceType::${DeviceType}, ScalarType::Long)'),
    'real': CodeTemplate('${arg_name}.to${ScalarName}()'),
    'accreal': CodeTemplate('${arg_name}.to${AccScalarName}()'),
    'TensorList': CodeTemplate(
            'checked_dense_tensor_list_unwrap(${arg_name},"${arg_name}",${arg_pos}, '
            'DeviceType::${DeviceType}, ${scalar_type})'),
    'IntArrayRef': CodeTemplate('check_intlist<${size}>(${arg_name}, "${arg_name}", ${arg_pos})')
}

CHECKED_USE = {
    'THTensor*': '{}_',
    'THIndexTensor*': '{}_',
    'THByteTensor*': '{}_',
    'THBoolTensor*': '{}_',
    'TensorList': "{0}_.data(), {0}_.size()",
}

CHECKED_USE_NULLABLE = CodeTemplate('${arg_name}_ ? ${usage} : NULL')

ALLOC_NOARGS_WRAP = {
    'THTensor*': 'c10::make_intrusive<TensorImpl, UndefinedTensorImpl>'
                 '(c10::Storage(c10::Storage::use_byte_size_t(), 0, allocator(), true),'
                 'DispatchKey::${Backend}, scalarTypeToTypeMeta(${ScalarName})).release()',
    'THByteTensor*': 'c10::make_intrusive<TensorImpl, UndefinedTensorImpl>'
                     '(c10::Storage(c10::Storage::use_byte_size_t(), 0, allocator(), true),'
                     'DispatchKey::${Backend}, scalarTypeToTypeMeta(ScalarType::Byte)).release()',
    'THBoolTensor*': 'c10::make_intrusive<TensorImpl, UndefinedTensorImpl>'
                     '(c10::Storage(c10::Storage::use_byte_size_t(), 0, allocator(), true),'
                     'DispatchKey::${Backend}, scalarTypeToTypeMeta(ScalarType::Bool)).release()',
    'THIndexTensor*': 'c10::make_intrusive<TensorImpl, UndefinedTensorImpl>'
                     '(c10::Storage(c10::Storage::use_byte_size_t(), 0, allocator(), true),'
                     'DispatchKey::${Backend}, scalarTypeToTypeMeta(ScalarType::Long)).release()',
}

# Replacements for constants when calling into TH
CONSTANT_REPLACEMENTS = [
    ('AS_REAL', '${ScalarType}'),
]

# Replacements for constants in header file function definitions
HEADER_CONSTANT_REPLACEMENTS = [
    (r'AS_REAL\((.*)\)', r'\1'),
]


class nested_dict(object):
    def __init__(self, base, parent):
        self.base, self.parent = base, parent

    def __getitem__(self, x):
        r = self.base.get(x)
        if r is not None:
            return r
        return self.parent[x]


Environment = TypedDict('Environment', {
    'state': str,
    'ScalarType': str,
    'ScalarName': str,
    'THTensor': str,
    'THType': str,
    'Backend': str,
    'DeviceType': str,
    'AccScalarName': str,
})

TopEnvironment = TypedDict('TopEnvironment', {
    'type_registrations': List[str],
    'type_headers': List[str],
    'function_registrations': List[str],
    'aten_ops': List[str],
    'type_method_declarations': List[str],
    'type_method_definitions': List[str],
    'tensor_method_declarations': List[str],
    'tensor_method_definitions': List[str],
    'function_declarations': List[str],
    'function_definitions': List[str],
    'type_ids': List[str],
    'native_function_declarations': List[str],
})

# A Declarations.cwrap formal argument
# type can contain THTensor* types
# NOTE: this must contain all 'AtFormal' attributes, because FunctionOption
# doesn't differentiate between whether we have AtFormals or THFormals
THFormal = TypedDict('THFormal', {
    'name': str,
    'type': str,
    'dynamic_type': str,
    'kwarg_only': bool,
    'is_nullable': bool,
    'default': str,
    'output': bool,
    'size': int,
    'annotation': str,
    'allocate': bool,
    'mask': bool,
}, total=False)

# Generic ATen formal or native_functions.yaml formal argument.
# type can contain Tensor& reference types.
AtFormal = TypedDict('AtFormal', {
    'name': str,
    'type': str,
    'dynamic_type': str,
    'kwarg_only': bool,
    'is_nullable': bool,
    'default': str,
    'output': bool,
    'size': int,
    'annotation': str,
}, total=False)

# Note [field_name versus name]
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# What is the difference between "field_name" and "name"?
#
# Return values of ATen operators always have a name: if it is not
# explicitly assigned a name inside native_functions.yaml like func:
# myop() -> (Tensor indices, Tensor value), then the codegen will
# automatically assign it a name like result0, or name might be
# specified inside Declarations.cwrap.  We don't want these assigned
# names to become part of the public API when we return a namedtuple for
# any such multiple-return function.
#
# Thus field_name is like name, but it is defined only when there is a
# name specified in native_functions.yaml. If field_name is defined,
# then the codegen would generate code to return namedtuple. Otherwise,
# it would just return tuple.

ReturnType = TypedDict('ReturnType', {
    'name': str,
    # See Note [field_name versus name]
    'field_name': str,
    'type': str,
    'dynamic_type': str,
}, total=False)

ReturnDecl = TypedDict('ReturnDecl', {
    'kind': str,
    'type': str,
    'arguments': List[int],
}, total=False)

# Represents a buffer in nn.yaml
NNBuffer = TypedDict('NNBuffer', {
    'name': str,
})

FunctionOption = TypedDict('FunctionOption', {
    'actuals': List[str],
    'schema_order_actuals': List[str],
    'api_name': str,
    # Like api_name, but it is the name of the internal
    # CPUType/CUDAType/TypeDefault function that wraps
    # the actual native call.  This name is NOT user
    # visible and is mangled with the overload name
    'type_wrapper_name': str,
    'arguments': List[THFormal],
    # 'schema_order_arguments' is like 'arguments' but keeps them in the
    # order they are defined in the JIT function schema while
    # 'arguments' does some modifications (e.g. reorders out arguments
    # and packs TensorOptions)
    'schema_order_arguments': List[THFormal],
    'backend_types': Dict[str, List[str]],
    'backends': List[str],
    'buffers': List[NNBuffer],
    # cimpls is really a List[FunctionOption]
    'cimpls': List[Any],
    'cname': str,
    # explicitly specify whether the function is a factory function or other special category
    'category_override': str,
    'condition': str,
    'device_guard': bool,
    'device_guard_declaration': str,
    'dispatch_scalar_type_declaration': str,
    'use_c10_dispatcher': str,
    'manual_kernel_registration': bool,
    'with_gil': bool,
    'cpu_half': bool,
    'cpu_bfloat16': bool,
    'cuda_bfloat16': bool,
    'deprecated': bool,
    'cpu_bool': bool,
    'cuda_bool': bool,
    # See Note [field_name versus name]
    'field_name': str,
    'formals_list': List[AtFormal],
    'formals_with_defaults': List[str],
    'formals': List[str],
    'formals_types': List[str],
    'cpp_signature': str,
    # 'schema_order_cpp_signature' is like 'cpp_signature' but keeps them in the
    # order they are defined in the JIT function schema while
    # 'cpp_signature' does some modifications (e.g. reorders out arguments
    # and packs TensorOptions)
    'schema_order_cpp_signature': str,
    'inplace': bool,
    'matches_jit_signature': bool,
    # This controls whether or not we generate the interface in Type or
    # TypeExtendedInterface
    'extended_method': bool,
    'method_actuals': List[str],
    'schema_order_method_actuals': List[str],
    'method_formals_with_defaults': List[str],
    'method_formals': List[str],
    'mode': str,
    'python_module': str,
    'name': str,
    'operator_name': str,
    'overload_name': str,
    'native_type_method_dispatch': str,
    # options should be List[FunctionOption]
    'options': Any,
    'schema_string': str,
    'return_call': str,
    'return_type': str,
    'return': ReturnDecl,
    'returns': List[ReturnType],
    'sparse': bool,
    'type_definition_body': List[str],
    'type_method_definition_dispatch': str,
    'variants': str,
})

OutputDeclaration = NamedTuple('OutputDeclaration', [
    ('name', str),
    ('operator_name', str),
    ('overload_name', str),
    ('use_c10_dispatcher', str),
    ('manual_kernel_registration', bool),
    ('category_override', str),
    ('matches_jit_signature', bool),
    ('schema_string', str),
    ('arguments', List[AtFormal]),
    # 'schema_order_arguments' is like 'arguments' but keeps them in the
    # order they are defined in the JIT function schema while
    # 'arguments' does some modifications (e.g. reorders out arguments
    # and packs TensorOptions)
    ('schema_order_arguments', List[AtFormal]),
    ('method_of', List[str]),
    ('mode', str),
    ('python_module', str),
    ('buffers', Optional[List[str]]),
    ('returns', List[ReturnType]),
    ('inplace', bool),
    ('is_factory_method', bool),
    ('abstract', bool),
    ('device_guard', bool),
    ('with_gil', bool),
    ('deprecated', bool),
])

FunctionCode = NamedTuple('FunctionCode', [
    ('definition', str),
    ('declaration', str),
])

OpRegistration = NamedTuple('OpRegistration', [
    ('operator_name', str),
    ('registration_code', str),
    ('schema_registration_code', str),
])


def device_guard(option, dispatch_options, dispatch_tensor):
    # For factory methods the `DeviceGuard` is already in the template.
    if option.get('device_guard', True):
        if dispatch_options:
            return 'const DeviceGuard device_guard({}.device());'.format(dispatch_options['name'])
        if dispatch_tensor:
            return 'const OptionalDeviceGuard device_guard(device_of({}));'.format(dispatch_tensor)
    return '// DeviceGuard omitted'


def dispatch_scalar_type(option, dispatch_options, dispatch_tensor):
    if dispatch_options:
        return 'auto dispatch_scalar_type = typeMetaToScalarType({}.dtype());'.format(dispatch_options['name'])
    if dispatch_tensor:
        return 'auto dispatch_scalar_type = infer_scalar_type({});'.format(dispatch_tensor)
    return '// dispatch_scalar_type omitted'


def is_real_argument_to_wrapper(argument):
    # type: (THFormal) -> bool
    return not argument.get('output', False) and\
        argument['type'] != 'CONSTANT' and\
        argument['type'] != 'argument'


def is_mutable_formal_argument(argument, option):
    # type: (THFormal, FunctionOption) -> bool
    return argument.get('output') or option['inplace'] and argument['name'] == 'self'


def check_methods_do_not_start_with_underscore(name, is_method):
    if name in {'_values', '_indices', '_nnz', '_dimI', '_dimV', '_coalesced_',
                '_version'}:
        return
    if is_method and name.startswith('_') and not name.startswith('__') and not name.startswith('_th_'):
        message = "Function '{}' starts with a single underscore and is ".format(name)
        message += "configured to have a method on Tensor. Functions that start with "
        message += " a single underscore should only be functions in the at:: "
        message += "namespace and not methods on Tensor!"
        raise RuntimeError(message)


def to_return_type(arg, option):
    # type: (THFormal, FunctionOption) -> ReturnType
    t = arg['type']
    rt = TYPE_RETURN.get(t, t)
    if rt == 'Tensor' and not arg.get('allocate'):
        rt = rt + ' &'
        if not is_mutable_formal_argument(arg, option):
            rt = 'const ' + rt
    return {
        'name': arg['name'],
        'type': rt,
        'dynamic_type': DYNAMIC_TYPE.get(arg['type'], arg['type']),
    }


def is_any_tensor_type(formal):
    return (formal['dynamic_type'] == 'Tensor' or formal['dynamic_type'] == 'ByteTensor'
            or formal['dynamic_type'] == 'IndexTensor' or formal['dynamic_type'] == 'BoolTensor')


def find_tensors(formals):
    # type: (List[AtFormal]) -> List[str]
    return [formal['name'] for formal in formals if is_any_tensor_type(formal)]


def find_tensorlists(formals):
    # type: (List[AtFormal]) -> List[str]
    return [formal['name'] for formal in formals if formal['dynamic_type'] == 'TensorList']


def find_dispatch_tensor(formals):
    # type: (List[AtFormal]) -> Optional[str]
    # Determine legacy TH-style single dispatch tensor.
    #
    # Also used to determine what tensor should be used to provide a default
    # DeviceGuard.  Unlike dispatch, we don't guard on ALL tensor arguments
    # (because this is not actually a thing you can do.)  Guarding on the
    # first argument is best effort to help people avoid doing this
    # themselves.

    for formal in formals:
        if formal['name'] == 'self' and is_any_tensor_type(formal) and not formal.get('is_nullable', False):
            return formal['name']
    # otherwise dispatch to the first Tensor or TensorList
    for formal in formals:
        if 'TensorList' == formal['dynamic_type'] or is_any_tensor_type(formal) and \
                not formal.get('is_nullable', False):
            return formal['name']

    return None


def is_multidispatch_formal(formal):
    # type: (AtFormal) -> bool
    return formal['dynamic_type'] in ['TensorOptions', 'TensorList'] or is_any_tensor_type(formal)


def find_multidispatch_formals(formals):
    # type: (List[AtFormal]) -> List[AtFormal]
    # Compute the list of all arguments which should be considered
    # for multiple dispatch.  Note that this doesn't completely replace
    # find_dispatch_tensor because we use the "dispatch tensor" to determine
    # device guards.  TensorOptions is included as part of this calculation.
    #
    # The interaction of multiple dispatch with TensorOptions
    # is quite interesting.  In particular, suppose I have:
    #
    #   cuda_tensor.new_like(1, device='cpu')
    #
    # Multiple dispatch will attempt a dispatch to CUDA, even though
    # the end tensor that should be produced here is a CPU one.  The
    # upshot is that if you have an operator with mixed TensorOptions
    # and Tensor arguments, you MUST only ever register it generically.
    return [f for f in formals if is_multidispatch_formal(f)]


def find_formal_by_type(formal_name, formals):
    # type: (str,List[AtFormal]) -> Optional[AtFormal]
    for formal in formals:
        if formal_name == formal['dynamic_type']:
            return formal
    return None


def format_formal(f):
    # type: (AtFormal) -> str
    return '{} {}'.format(f['type'], f['name'])


def formal_with_default(f):
    # type: (AtFormal) -> str
    s = format_formal(f)
    v = f.get('default')
    if v is None:
        return s
    if isinstance(v, bool):
        v = str(v).lower()
    return '{}={}'.format(s, v)


def gen_dispatch_key_init(var_name, formals):
    # type: (str, List[AtFormal]) -> List[str]
    topt_formals = []
    non_topt_formals = []
    for f in find_multidispatch_formals(formals):
        if f['dynamic_type'] == 'TensorOptions':
            topt_formals.append(f)
        else:
            non_topt_formals.append(f)

    if len(topt_formals) == 1 and non_topt_formals == []:
        topt = topt_formals[0]
        return ['DispatchKey {} = {}.computeDispatchKey();'.format(var_name, topt['name'])]

    subexprs = []
    for f in topt_formals:
        subexprs.append('DispatchKeySet({}.computeDispatchKey())'.format(f['name']))
    if non_topt_formals != []:
        args = ', '.join([f['name'] for f in non_topt_formals])
        subexprs.append('c10::detail::multi_dispatch_key_set({})'.format(args))
    return [
        'DispatchKeySet _dk_set = {};'.format(' | '.join(subexprs)),
        'DispatchKeySet _dk_mask = c10::DispatchKeySet(DispatchKeySet::FULL_AFTER, DispatchKey::BackendSelect);',
        'DispatchKey {} = c10::impl::dispatchTypeId(_dk_set, _dk_mask);'.format(var_name),
    ]


def is_factory(option):
    # type: (FunctionOption) -> bool
    formals = option['formals_list']
    return find_formal_by_type('TensorOptions', formals) is not None and 'method' not in option['variants']


def gen_device_init(option, backend_type_env):
    # type: (FunctionOption, Environment) -> List[str]
    # generate a device init statement, if the passed function option is a Tensor factory.
    #
    if is_factory(option):
        name = option['name']
        device_type = backend_type_env['DeviceType']
        if device_type == 'CUDA' or device_type == 'HIP':
            return ['globalContext().lazyInit{}();'.format(device_type)]
    return []

def create_generic(top_env, declarations):
    # type: (TopEnvironment, List[FunctionOption]) -> Tuple[List[OutputDeclaration], List[OpRegistration]]
    # translates defaults from cwrap types to C++ values
    def translate_default(argument, type_str, default):
        # type: (THFormal, str, Any) -> Any
        if default is None:
            # cause the default constructor for the object to run
            return '{}'
        for pattern, replacement in HEADER_CONSTANT_REPLACEMENTS:
            default = re.sub(pattern, replacement, str(default))
        if type_str in {'Scalar', 'int64_t', 'double'}:
            try:
                return int(default)
            except Exception:
                try:
                    return float(default)
                except Exception:
                    return default
        elif type_str == 'bool':
            assert default.lower() in ['true', 'false']
            return default.lower() == 'true'
        else:
            return default

    # change from THTensor* to Tensor & so we get how it will appear
    # in the aten argument list...
    def translate_formal(argument, option):
        # type: (THFormal, FunctionOption) -> AtFormal
        type_str = TYPE_FORMAL_GENERIC.get(argument['type'], argument['type'])
        if type_str == 'Tensor &' and not is_mutable_formal_argument(argument, option):
            type_str = 'const ' + type_str
        translated = {
            'name': argument['name'],
            'type': type_str,
            'dynamic_type': DYNAMIC_TYPE.get(argument['type'], argument['type']),
        }  # type: AtFormal
        if 'default' in argument:
            default = translate_default(argument, type_str, argument['default'])
            translated['default'] = default
        if argument.get('output'):
            translated['output'] = True
        if argument.get('size'):
            translated['size'] = argument['size']
        if argument.get('is_nullable') is not None:
            translated['is_nullable'] = argument['is_nullable']
        return translated

    def get_formals(option, schema_order, include_constants=False):
        # type: (FunctionOption, bool, bool) -> List[AtFormal]
        seen = set()  # type: Set[str]
        pos_args = []  # type: List[THFormal]
        kwd_args = []  # type: List[THFormal]

        def insert(argument):
            # type: (THFormal) -> None
            if argument['name'] not in seen:
                seen.add(argument['name'])
                # there are no kwarg_only THFormals
                pos_args.append(argument)

        def has_output_mask(argument):
            # type: (THFormal) -> bool
            return argument.get('allocate', False) and argument.get('mask', False)

        if schema_order:
            arguments = copy.deepcopy(option['schema_order_arguments'])
        else:
            arguments = copy.deepcopy(option['arguments'])
        for argument in arguments:
            if argument.get('output') and not argument.get('allocate', False):
                insert(argument)
        for argument in arguments:
            if include_constants and argument['type'] == 'CONSTANT':
                insert(argument)
            elif is_real_argument_to_wrapper(argument):
                insert(argument)
        if any(has_output_mask(arg) for arg in arguments):
            mask_size = sum(has_output_mask(arg) for arg in arguments)
            insert({
                'name': 'output_mask',
                # NB: Lack of space in comma works around parsing
                # problem in gen_variable_type.py
                'type': 'std::array<bool,{}>'.format(mask_size),
                'default': '{{' + ', '.join(['true'] * mask_size) + '}}',
            })

        result = pos_args + kwd_args
        return [translate_formal(argument, option) for argument in result]

    def get_return_types(option):
        # type: (FunctionOption) -> List[ReturnType]
        ret = option['return']
        if ret['kind'] == 'arguments':
            argument_indices = ret['arguments']
            if len(argument_indices) == 1:
                the_arg = option['arguments'][argument_indices[0]]
                return [to_return_type(the_arg, option)]
            else:
                return [to_return_type(option['arguments'][idx], option)
                        for idx in argument_indices]
        elif ret['kind'] == 'type':
            return [{
                'type': TYPE_RETURN.get(ret['type'], ret['type']),
                'dynamic_type': DYNAMIC_TYPE.get(ret['type'], ret['type']),
            }]
        else:
            raise Exception("format_return_type")

    def format_return_type(return_types):
        # type: (List[ReturnType]) -> str
        if len(return_types) == 0:
            return "void"
        elif len(return_types) == 1:
            return return_types[0]['type']
        return "std::tuple<{}>".format(','.join(r['type'] for r in return_types))

    def process_schema_order_actual(schema_order_actual):
        if schema_order_actual == 'dtype':
            return 'optTypeMetaToScalarType(options.dtype_opt())'
        elif schema_order_actual == 'layout':
            return 'options.layout_opt()'
        elif schema_order_actual == 'device':
            return 'options.device_opt()'
        elif schema_order_actual == 'pin_memory':
            return 'options.pinned_memory_opt()'
        elif schema_order_actual == 'memory_format':
            return 'c10::impl::check_tensor_options_and_extract_memory_format(options, memory_format)'
        else:
            return schema_order_actual

    def process_legacy_th_option(option):
        # type: (FunctionOption) -> None
        # Mutably populate option with derived values computed from values
        # passed in to option.
        option['inplace'] = re.search(
            '(^__i|[^_]_$)', option['api_name']) is not None

        # print(yaml.dump(option))
        formals = get_formals(option, False)
        schema_order_formals = get_formals(option, True)
        option['formals_list'] = formals
        option['formals'] = [format_formal(f) for f in formals]
        option['formals_with_defaults'] = [formal_with_default(f) for f in formals]
        option['returns'] = get_return_types(option)
        option['return_type'] = format_return_type(option['returns'])
        option['return_call'] = 'return ' if option['return_type'] != 'void' else ''
        option['actuals'] = [f['name'] for f in formals]

        option['method_formals'] = [format_formal(f) for f in formals
                                    if f['name'] != 'self']
        option['method_formals_with_defaults'] = (
            [formal_with_default(f) for f in formals if f['name'] != 'self'])
        # *this is 'const Tensor&' since all Tensor methods are const and must
        # be const_casted to be accepted as native function's non-const argument
        option['method_actuals'] = [
            f['name'] if f['name'] != 'self' else 'const_cast<Tensor&>(*this)' for f in formals]

        assert 'method' not in option['variants'], 'TH functions cannot be methods'
        is_function = 'function' in option['variants']
        # NB: TH functions don't support multiple dispatch
        dispatch_tensor = find_dispatch_tensor(formals)
        is_namespace_function = is_function and dispatch_tensor is not None

        if option['mode'] == 'TH':
            option['device_guard'] = False
        option['device_guard_declaration'] = device_guard(option, False, dispatch_tensor)
        option['dispatch_scalar_type_declaration'] = dispatch_scalar_type(option, False, dispatch_tensor)

        assert option['extended_method'], 'Expected legacy operator to be an extended method'

    def native_get_formals(option, schema_order, include_constants=False):
        # type: (FunctionOption, bool, bool) -> List[AtFormal]
        seen = set()  # type: Set[str]
        pos_args = []
        kwd_args = []

        def insert(argument):
            # type: (AtFormal) -> None
            if argument['name'] not in seen:
                seen.add(argument['name'])
                if argument.get('kwarg_only', False):
                    kwd_args.append(argument)
                else:
                    pos_args.append(argument)

        if schema_order:
            arguments = option['schema_order_arguments']
        else:
            arguments = option['arguments']
        for argument in arguments:
            insert(argument)

        # not clear we need dynamic_type translation as we can specify the correct type
        # directly in native functions
        def add_dynamic_type(argument, option):
            # type: (AtFormal, FunctionOption) -> AtFormal
            argument['dynamic_type'] = NATIVE_DYNAMIC_TYPE.get(argument['type'], argument['type'])
            return argument

        result = pos_args + kwd_args
        result = [add_dynamic_type(argument, option) for argument in result]

        # ensure we get reference-type formals when appropriate
        def native_translate_formals(argument, option):
            # type: (AtFormal, FunctionOption) -> AtFormal
            def translate_map(const):
                # type: (bool) -> Dict[str, str]
                return {
                    'Tensor': 'const Tensor &' if const else 'Tensor &',
                    'Type': 'const Type &' if const else 'Type &',
                    'TensorOptions': 'const TensorOptions &' if const else 'TensorOptions &',
                    'TensorList': 'TensorList',
                }

            if argument.get('is_nullable') and argument['type'] not in translate_map(False).keys():
                argument['type'] = "c10::optional<{}>".format(argument['type'])

            # Note: the 'self' trap is here only to preserve the const arg 0 for set_data.
            # I.e., the signature of the cpp implementation currently fits the code
            # generated from a misread schema, but the alias annotation is the truth.
            # TODO fix the signature of set_data's cpp impl to match correct codegen from
            # the current schema.
            # then remove this
            if argument['name'] == 'self':
                is_mutable = option['inplace']
            else:
                is_mutable = '!' in (argument['annotation'] or '')

            if is_mutable:
                argument['type'] = translate_map(False).get(argument['type'], argument['type'])
            else:
                argument['type'] = translate_map(True).get(argument['type'], argument['type'])

            return argument

        result = [native_translate_formals(argument, option) for argument in result]
        return result

    # this can return multiple return types in a list, e.g. ['Tensor', 'Tensor']
    def native_get_return_types(option):
        # type: (FunctionOption) -> List[ReturnType]
        ret = option['return']

        return_types = []  # List[ReturnType]
        for t_raw in ret:
            # See Note [field_name versus name]
            field_name = None
            if isinstance(t_raw, str):
                t = t_raw
                name = None
            else:
                t = t_raw['type']
                name = t_raw['name']
                if 'field_name' in t_raw:
                    field_name = t_raw['field_name']

            # can't actually return a TensorList (since it's a reference object)
            actual_return_type = {'TensorList': 'std::vector<Tensor>'}.get(t, t)

            if actual_return_type == 'Tensor' and (option['inplace'] or option['api_name'].endswith('_out')):
                # follow normal ATen convention of returning Tensor & for inplace functions.
                actual_return_type = 'Tensor &'

            rtype = {
                'type': actual_return_type,
                'dynamic_type': NATIVE_DYNAMIC_TYPE.get(t, t),
            }  # type: ReturnType
            if name is not None:
                rtype['name'] = name
            if field_name is not None:
                rtype['field_name'] = field_name
            return_types.append(rtype)

        return return_types

    def process_native(option):
        # type: (FunctionOption) -> Optional[OutputDeclaration]
        assert option['python_module'] == '' or option['python_module'] == 'nn', \
            "Found python_module of {} for decl {}, but only \'\' string or \'nn\' are supported".format(
                option['python_module'], option['name'])
        formals = native_get_formals(option, False)
        schema_order_formals = native_get_formals(option, True)
        option['formals_list'] = formals
        option['formals'] = [format_formal(f) for f in formals]
        option['formals_with_defaults'] = [formal_with_default(f) for f in formals]
        option['returns'] = native_get_return_types(option)
        option['return_type'] = format_return_type(option['returns'])
        option['return_call'] = 'return ' if option['return_type'] != 'void' else ''
        option['actuals'] = [f['name'] for f in formals]
        option['schema_order_actuals'] = [f['name'] for f in schema_order_formals]

        option['formals_types'] = [f['type'] for f in option['formals_list']]

        option['cpp_signature'] = "{} ({})".format(option['return_type'], ", ".join(option['formals_types']))
        option['schema_order_cpp_signature'] = "{} ({})".format(
            option['return_type'],
            ", ".join([f['type'] for f in schema_order_formals]))

        option['method_formals'] = [format_formal(f) for f in formals
                                    if f['name'] != 'self']
        option['method_formals_with_defaults'] = (
            [formal_with_default(f) for f in formals if f['name'] != 'self'])
        # *this is 'const Tensor&' since all Tensor methods are const and must
        # be const_casted to be accepted as native function's non-const argument
        option['method_actuals'] = [
            f['name'] if f['name'] != 'self' else 'const_cast<Tensor&>(*this)' for f in formals]
        option['schema_order_method_actuals'] = [
            f['name'] if f['name'] != 'self' else 'const_cast<Tensor&>(*this)' for f in schema_order_formals]

        if find_formal_by_type('TensorOptions', formals) is not None:
            option['schema_order_actuals'] = [
                process_schema_order_actual(actual) for actual in option['schema_order_actuals']]
            option['schema_order_method_actuals'] = [
                process_schema_order_actual(actual) for actual in option['schema_order_method_actuals']]

        def gen_tensor_method(option, formals):
            # type: (Any, List[AtFormal]) -> FunctionCode
            def swizzle_self(f):  # blegh
                if f['name'] == 'self':
                    fc = f.copy()
                    fc['name'] = '*this'
                    return fc
                else:
                    return f

            dispatch_key_var_name = '_dk'
            dispatch_key_init = gen_dispatch_key_init(dispatch_key_var_name, [swizzle_self(f) for f in formals])

            if isinstance(type_method_dispatch, dict):
                static_dispatch_function_cases = []
                # NB: As this code is currently written, there will NEVER be
                # a backend generated for variable dispatch.  There is nothing
                # stopping us from actually implementing this, however, if you
                # really wanted variable on mobile, there's nothing stopping
                # you from implementing this (however, you would have an
                # annoying phase problem, since code generation for variable
                # happens in tools/ which happens later than here.)
                #
                # If you pass in a variable to the dispatch, and variable is
                # enabled, this switch will fail.  This is intentional: you
                # probably need to disable variable globally in the mobile
                # calling code.
                for backend in static_dispatch_backends:
                    if backend in type_method_dispatch:
                        static_dispatch_function_case = STATIC_DISPATCH_FUNCTION_SWITCH_CASE.substitute(
                            option,
                            backend=backend,
                            backend_function=type_method_dispatch[backend],
                            actuals=option['method_actuals'])
                        if (backend in static_dispatch_backends_ifdef_guard):
                            static_dispatch_function_cases.append(IFDEF_BLOCK.substitute(
                                option,
                                ifdef_guard=static_dispatch_backends_ifdef_guard[backend],
                                content=static_dispatch_function_case))
                        else:
                            static_dispatch_function_cases.append(static_dispatch_function_case)

                static_dispatch_method_body = STATIC_DISPATCH_FUNCTION_SWITCH_BODY.substitute(
                    option,
                    dispatch_key_var_name=dispatch_key_var_name,
                    dispatch_key_init=dispatch_key_init,
                    static_dispatch_function_cases=static_dispatch_function_cases)
            else:
                static_dispatch_method_body = STATIC_DISPATCH_FUNCTION_DEFAULT_BODY.substitute(
                    option, actuals=option['method_actuals'])

            # See NOTE[UnboxedOnly]
            if option['use_c10_dispatcher'] == 'full':
                tensor_method_actuals = option['schema_order_method_actuals']
                tensor_method_cpp_signature = option['schema_order_cpp_signature']
            else:
                assert option['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
                tensor_method_actuals = option['method_actuals']
                tensor_method_cpp_signature = option['cpp_signature']

            method_definition = TENSOR_METHOD_DEFINITION.substitute(
                option, static_dispatch_method_body=static_dispatch_method_body,
                tensor_method_actuals=tensor_method_actuals,
                tensor_method_cpp_signature=tensor_method_cpp_signature
            )
            return FunctionCode(
                declaration=TENSOR_METHOD_DECLARATION.substitute(
                    option, static_dispatch_method_body=static_dispatch_method_body),
                definition=method_definition)

        def gen_namespace_function(option, multidispatch_formals):
            # type: (Any, List[AtFormal]) -> FunctionCode

            dispatch_key_var_name = '_dk'
            dispatch_key_init = gen_dispatch_key_init(dispatch_key_var_name, formals)

            declaration = DEPRECATED_FUNCTION_DECLARATION if option['deprecated'] else FUNCTION_DECLARATION
            fn_declaration = declaration.substitute(option)

            if isinstance(type_method_dispatch, dict):
                static_dispatch_function_cases = []
                for backend in static_dispatch_backends:
                    if backend in type_method_dispatch:
                        static_dispatch_function_case = STATIC_DISPATCH_FUNCTION_SWITCH_CASE.substitute(
                            option,
                            backend=backend,
                            backend_function=type_method_dispatch[backend],
                            actuals=option['actuals'])
                        if (backend in static_dispatch_backends_ifdef_guard):
                            static_dispatch_function_cases.append(IFDEF_BLOCK.substitute(
                                option,
                                ifdef_guard=static_dispatch_backends_ifdef_guard[backend],
                                content=static_dispatch_function_case))
                        else:
                            static_dispatch_function_cases.append(static_dispatch_function_case)
                static_dispatch_function_body = STATIC_DISPATCH_FUNCTION_SWITCH_BODY.substitute(
                    option,
                    dispatch_key_var_name=dispatch_key_var_name,
                    dispatch_key_init=dispatch_key_init,
                    static_dispatch_function_cases=static_dispatch_function_cases)
            else:
                static_dispatch_function_body = STATIC_DISPATCH_FUNCTION_DEFAULT_BODY.substitute(
                    option, actuals=option['actuals'])

            # See NOTE[UnboxedOnly]
            if option['use_c10_dispatcher'] == 'full':
                function_actuals = option['schema_order_actuals']
                function_cpp_signature = option['schema_order_cpp_signature']
            else:
                assert option['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
                function_actuals = option['actuals']
                function_cpp_signature = option['cpp_signature']

            fn_definition = FUNCTION_DEFINITION.substitute(
                option, static_dispatch_function_body=static_dispatch_function_body,
                function_actuals=function_actuals,
                function_cpp_signature=function_cpp_signature)

            return FunctionCode(definition=fn_definition, declaration=fn_declaration)

        assert find_formal_by_type('Type', formals) is None, \
            "Found Type argument in {}({}). Use TensorOptions instead.".format(
                option['name'], ", ".join(option['method_formals_with_defaults']))

        type_method_dispatch = option['type_method_definition_dispatch']

        is_method = 'method' in option['variants']
        is_namespace_function = 'function' in option['variants']
        # For method-only entries, the first argument should be self
        if is_method and not is_namespace_function:
            assert formals[0]['name'] == 'self'
        is_factory_method = is_factory(option)

        check_methods_do_not_start_with_underscore(option['name'], is_method)

        # NB: Device guard and scalar type generated code is still based on the
        # first argument.  Scalar type test will be removed once TH is removed.
        # If you need more complex device guard behavior, you should disable
        # device guard and then manually add the guards you need.
        dispatch_options = find_formal_by_type('TensorOptions', formals)
        guard_tensor = None if dispatch_options else find_dispatch_tensor(formals)
        option['device_guard_declaration'] = device_guard(option, dispatch_options, guard_tensor)
        option['dispatch_scalar_type_declaration'] = dispatch_scalar_type(option, dispatch_options, guard_tensor)

        top_env['aten_ops'].append(OPERATOR_NAME_FULL.substitute(option))

        option['native_type_method_dispatch'] = type_method_dispatch

        # Note [Abstract ATen methods]
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # An abstract ATen method is one whose dispatch differs between
        # types.  These are implemented in derived types (with a
        # standard (throwing) definition in Type).  A concrete ATen
        # method is one which has the same dispatch for all types;
        # we just implement it in the base Type.  This is exposed
        # in Declarations.yaml via a field named 'abstract'.
        abstract = False
        op_registrations.append(OpRegistration(
            operator_name=OPERATOR_NAME.substitute(option),
            registration_code=SCHEMA_REGISTRATION.substitute(option),
            schema_registration_code=SCHEMA_REGISTRATION.substitute(option)))
        if isinstance(type_method_dispatch, dict):
            abstract = True
            # Having manual_kernel_registration for an abstract method doesn't make sense.
            assert not option['manual_kernel_registration']
        else:
            top_env['type_method_declarations'].append(NATIVE_DISPATCH_DECLARATION.substitute(option))
            top_env['type_method_definitions'].append(NATIVE_DISPATCH_DEFINITION_DEFAULT.substitute(option))
            if not option['manual_kernel_registration']:
                # See NOTE[UnboxedOnly]
                if option['use_c10_dispatcher'] == 'full':
                    op_registrations.append(OpRegistration(
                        operator_name=OPERATOR_NAME.substitute(option),
                        registration_code=DEFAULT_FUNCTION_REGISTRATION.substitute(option),
                        schema_registration_code=SCHEMA_REGISTRATION.substitute(option)))
                else:
                    assert option['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
                    op_registrations.append(OpRegistration(
                        operator_name=OPERATOR_NAME.substitute(option),
                        registration_code=DEFAULT_UNBOXEDONLY_FUNCTION_REGISTRATION.substitute(option),
                        schema_registration_code=SCHEMA_REGISTRATION.substitute(option)))

        # generate the at::native function declarations (i.e. what the user will implement)
        if isinstance(type_method_dispatch, dict):
            generated_native_functions = []  # type: List[str]
            for key in sorted(type_method_dispatch.keys()):
                value = type_method_dispatch[key]
                # skip functions in different namespace, e.g. legacy::cpu
                if "::" in value:
                    continue
                if value not in generated_native_functions:
                    option['native_type_method_dispatch'] = value
                    top_env['native_function_declarations'].append(NATIVE_DECLARATION.substitute(option))
                    generated_native_functions.append(value)
        else:
            top_env['native_function_declarations'].append(NATIVE_DECLARATION.substitute(option))

        method_of = ['Type']
        if is_method:
            code = gen_tensor_method(option, formals)
            top_env['tensor_method_declarations'].append(code.declaration)
            top_env['tensor_method_definitions'].append(code.definition)
            method_of.append('Tensor')

        if is_namespace_function:
            code = gen_namespace_function(option, formals)
            top_env['function_definitions'].append(code.definition)
            top_env['function_declarations'].append(code.declaration)
            method_of.append('namespace')

        return OutputDeclaration(
            name=option['api_name'],
            operator_name=option['operator_name'],
            overload_name=option['overload_name'],
            use_c10_dispatcher=option['use_c10_dispatcher'],
            manual_kernel_registration=option['manual_kernel_registration'],
            category_override=option['category_override'],
            matches_jit_signature=option["matches_jit_signature"],
            schema_string=option["schema_string"],
            arguments=formals,
            schema_order_arguments=schema_order_formals,
            method_of=method_of,
            mode=option['mode'],
            python_module=option['python_module'],
            buffers=None,
            returns=option['returns'],
            inplace=option['inplace'],
            is_factory_method=is_factory_method,
            # See Note [Abstract ATen methods]
            abstract=abstract,
            device_guard=option.get('device_guard', True),
            with_gil=option.get('with_gil', False),
            deprecated=option['deprecated'],
        )

    output_declarations = []  # type: List[OutputDeclaration]
    op_registrations = []  # type: List[OpRegistration]
    for declaration in declarations:
        output_options = []  # type: List[OutputDeclaration]
        for option in declaration['options']:
            option["matches_jit_signature"] = declaration["matches_jit_signature"]
            option["schema_string"] = declaration["schema_string"]
            try:
                if option['mode'] != 'native':
                    # Mutably populate option with values
                    process_legacy_th_option(option)
                else:
                    output_option = process_native(option)
                    if output_option:
                        output_options.append(output_option)
            except NYIError:
                option['skip'] = True
        output_declarations.extend(output_options)

    return output_declarations, op_registrations


def create_derived(backend_type_env, declarations):
    # type: (Environment, List[FunctionOption]) -> Tuple[List[str], List[str], List[OpRegistration], List[str], List[str]]
    type_object_declarations = []  # type: List[str]
    type_object_definitions = []  # type: List[str]
    op_registrations = []  # type: List[OpRegistration]
    legacy_th_declarations = []  # type: List[str]
    legacy_th_definitions = []  # type: List[str]
    is_cuda = 'CUDA' in backend_type_env['Backend']

    def requires_checked_cast(argument):
        # type: (THFormal) -> bool
        if argument['type'] == 'IntArrayRef':
            return 'size' in argument
        return argument['type'] in CHECKED_CAST

    def nullable_argument(argument):
        # type: (THFormal) -> bool
        return argument.get('is_nullable', False)

    def get_argument(env, argument, option):
        # type: (Environment, THFormal, FunctionOption) -> str
        if requires_checked_cast(argument):
            checked_use = CHECKED_USE.get(
                argument['type'], '{}_').format(argument['name'])
            if nullable_argument(argument):
                checked_use = CHECKED_USE_NULLABLE.substitute(
                    env={}, arg_name=argument['name'], usage=checked_use)
            return checked_use
        elif argument['type'] == 'CONSTANT':
            v = str(argument.get('default', argument['name']))
            for pattern, replacement in CONSTANT_REPLACEMENTS:
                v = re.sub(pattern, replacement, v)
            return CodeTemplate(v).substitute(env)
        # e.g. argument 0, i.e. repeat the 0th argument in this position...
        elif argument['type'] == 'argument':
            index = int(argument['name'])
            return get_argument(env, option['arguments'][index], option)
        else:
            return argument['name']

    def get_arguments(env, arguments, option):
        # type: (Environment, List[THFormal], FunctionOption) -> List[str]
        return [get_argument(env, argument, option)
                for argument in arguments]

    def allocate_arg(arg, output_count, backend, scalar_name):
        # type: (THFormal, int, str, str) -> List[str]
        name = arg['name']
        allocation = CodeTemplate(ALLOC_NOARGS_WRAP[arg['type']]).substitute(Backend=backend, ScalarName=scalar_name)
        tensor_arg = '{}_'.format(name)
        if arg.get('mask', False):
            allocation = 'output_mask[{}] ? {} : nullptr'.format(output_count, allocation)
            tensor_arg = ('{}_ == nullptr ? (TensorImpl*)UndefinedTensorImpl::singleton() : (TensorImpl*){}_'
                          .format(name, name))
        intrusive_ptr_type = 'c10::intrusive_ptr<TensorImpl, UndefinedTensorImpl>'
        return [
            'auto {}_ = {};'.format(name, allocation),
            'auto {} = Tensor({}::reclaim({}));'.format(name, intrusive_ptr_type, tensor_arg),
        ]

    def handle_call(env, option, cimpl):
        # type: (Environment, FunctionOption, FunctionOption) -> str
        is_nn = option['mode'] == 'NN'
        actuals = get_arguments(env, cimpl['arguments'], option)
        if is_cuda or is_nn:
            actuals = ['globalContext().getTHCState()'] + actuals

        cname = cimpl['cname']
        if option.get('sparse', False):
            if is_cuda:
                cname = 'THCS' + env['ScalarName'] + "Tensor_" + cname
            else:
                cname = env['THTensor'].replace('TH', 'THS') + '_' + cname
        elif is_nn:
            cname = 'THNN_{}'.format(env['THType']) + cname
        else:
            cname = env['THTensor'] + '_' + cname

        call = CALL_TEMPLATE.substitute(actuals=actuals, cname=cname)
        if cimpl.get('condition') is not None:
            call = 'if ({}) {}'.format(cimpl['condition'], call)
        return call

    def emit_body(env, option, scalar_type_cases):
        # type: (Environment, FunctionOption, List[str]) -> List[str]
        body = []  # type: List[str]

        switch_prologue = []  # type: List[str]
        output_count = 0
        cases = []

        for arg in option['arguments']:
            # make a new allocation of TensorImpl, then wrap a Tensor around it.
            if arg.get('allocate', False):
                switch_prologue += allocate_arg(arg, output_count, env['Backend'], 'dispatch_scalar_type')
                output_count += 1

        for scalar_name, c_type, accreal, _ in scalar_types:
            if scalar_name in scalar_type_cases:
                case_body = []  # type: List[str]
                # arguments are potentially duplicated because of one argument
                # referencing another
                seen_names = set()  # type: Set[str]
                count = 0

                case_env = {
                    'Backend': env['Backend'],
                    'DeviceType': env['DeviceType'],
                    'state': env['state'],
                    'ScalarType': c_type,
                    'ScalarName': scalar_name,
                    'AccScalarName': accreal,
                    'THType': scalar_name,
                    'THTensor': 'TH{}Tensor'.format(scalar_name)
                }  # type: Environment
                if case_env['Backend'] == 'CUDA':
                    sname = '' if scalar_name == "Float" else scalar_name
                    case_env['THType'] = 'Cuda{}'.format(sname)
                    case_env['THTensor'] = 'THCuda{}Tensor'.format(sname)

                for arg in option['arguments']:
                    if is_real_argument_to_wrapper(arg):
                        count += 1

                    # only generated checked casts the first time we see it
                    if arg['name'] not in seen_names and requires_checked_cast(arg):
                        seen_names.add(arg['name'])

                        # make a new allocation of TensorImpl, then wrap a Tensor around it.
                        if not arg.get('allocate', False):
                            # special case where we allow undefined Tensors, and thus
                            # the checked cast succeeds even if the Tensor is not
                            # defined
                            null_okay = 'true' if nullable_argument(arg) else 'false'

                            # extract the TensorImpl from an existing tensor
                            check_cast = CHECKED_CAST[arg['type']].substitute(
                                case_env, arg_name=arg['name'], arg_pos=count,
                                api_name=option['api_name'], null_okay=null_okay,
                                size=arg.get('size'), scalar_type='dispatch_scalar_type')
                            case_body.append("auto {}_ = {};".format(
                                arg['name'], check_cast))

                # cimpls, if it exists, contains the underlying C function names and
                # arguments. Otherwise use option
                cimpls = option.get('cimpls', [option])
                calls = [handle_call(case_env, option, cimpl) for cimpl in cimpls]

                ret = option['return']

                if ret['kind'] == 'arguments':
                    case_body.extend([call + ';' for call in calls])
                    # return handled later
                elif ret['kind'] == 'type':
                    assert len(calls) == 1
                    call = calls[0]

                    # return the same underlying Tensor type for both real and accreal; this ensures
                    # e.g. x.sum(0) and x.sum() return the same type. We explicitly cast to the
                    # ScalarType before constructing the scalar_tensor to avoid overflow checking.
                    if ret['type'] == 'accreal' or ret['type'] == 'real':
                        return_scalar = ('return at::scalar_tensor(convert<${ScalarType}>(${call}), '
                                         'options(ScalarType::${ScalarName}));')
                        case_body.append(CodeTemplate(return_scalar).substitute(case_env, call=call))
                    else:
                        case_body.append("return {};".format(call))
                else:
                    raise Exception("NYI - return handling")

                cases.append(LEGACY_TH_DEFINITION_CASE.substitute(case_env, case_body=case_body))
        switch_epilogue = ''
        if ret['kind'] == 'arguments':
            arguments_indices = ret['arguments']
            arguments = [option['arguments'][argi]
                         for argi in arguments_indices]
            if len(arguments_indices) == 1:
                arg = arguments[0]
                switch_epilogue = "return {};".format(arg['name'])
            else:
                types = [to_return_type(arg, option)['type']
                         for arg in arguments]
                # TODO: check for move semantics...
                names = [arg['name'] for arg in arguments]
                switch_epilogue = CodeTemplate("return std::tuple<${types}>(${names});").substitute(
                    types=types, names=names)
        body.append(LEGACY_TH_DEFINITION_SWITCH_STATEMENT.substitute(env, cases=cases,
                                                                     switch_prologue=switch_prologue,
                                                                     switch_epilogue=switch_epilogue))
        return body

    def process_legacy_th_option(option):
        # type: (FunctionOption) -> None
        backend = backend_type_env['Backend']
        if backend in option['backend_types']:
            env = nested_dict(option, backend_type_env)
            body = emit_body(env, option, option['backend_types'][backend])  # type: ignore
            option['type_definition_body'] = body
            legacy_th_declarations.append(
                LEGACY_TH_DECLARATION.substitute(env))
            legacy_th_definitions.append(
                LEGACY_TH_DEFINITION.substitute(env))

    def process_native(option):
        # type: (FunctionOption) -> None
        dispatch = option['type_method_definition_dispatch']
        env = nested_dict(option, backend_type_env)

        if isinstance(dispatch, dict):
            # If we're here, then our native_functions.yaml entry has dispatch configuration.
            # Having manual kernel registration doesn't make sense.
            assert not option['manual_kernel_registration']

            backend = backend_type_env['Backend']
            if backend in option['backend_types']:

                native_dispatch = dispatch.get(backend)

                type_object_declarations.append(
                    NATIVE_DISPATCH_DECLARATION.substitute(env))

                option['native_type_method_dispatch'] = native_dispatch
                option['device_init'] = gen_device_init(option, backend_type_env)

                if backend in ['CPU', 'SparseCPU', 'QuantizedCPU', 'MkldnnCPU']:
                    # Omit the device guard entirely in these cases
                    def_backend = NATIVE_DISPATCH_DEFINITION_CPU_BACKEND
                else:
                    def_backend = NATIVE_DISPATCH_DEFINITION_GENERIC_BACKEND

                type_object_definitions.append(def_backend.substitute(env))

                if native_dispatch:
                    # See NOTE[UnboxedOnly]
                    if option['use_c10_dispatcher'] == 'full':
                        op_registrations.append(OpRegistration(
                            operator_name=OPERATOR_NAME.substitute(option),
                            registration_code=BACKEND_FUNCTION_REGISTRATION.substitute(env),
                            schema_registration_code=SCHEMA_REGISTRATION.substitute(option)))
                    else:
                        assert option['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
                        op_registrations.append(OpRegistration(
                            operator_name=OPERATOR_NAME.substitute(option),
                            registration_code=BACKEND_UNBOXEDONLY_FUNCTION_REGISTRATION.substitute(env),
                            schema_registration_code=SCHEMA_REGISTRATION.substitute(option)))

    for declaration in declarations:
        for option in declaration['options']:
            if not option.get('skip', False):
                try:
                    if option['mode'] == 'NN' and option.get('cimpls') is None:
                        continue
                    if option['mode'] != 'native':
                        process_legacy_th_option(option)
                    else:
                        process_native(option)
                except NYIError:
                    pass
    return (type_object_declarations, type_object_definitions, op_registrations,
            legacy_th_declarations, legacy_th_definitions)
