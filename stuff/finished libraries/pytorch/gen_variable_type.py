# Generates VariableType.h/cpp
#
# VariableType is a subclass of at::Type that provides the binding code
# necessary to provide a differentiable version of ATen operators. There are a
# number of different things we could mean:
#
#   - Given a non-differentiable forward implementation, we might
#     directly associate it with a backward implementation to make
#     it differentiable.  This is the common case.
#
#   - Some functions don't need a backwards implementation, because
#     backpropagation will never propagate beyond them.  There are a
#     number of different reasons why this may be the case:
#
#       - The function has no differentiable inputs
#       - The function's output is not differentiable
#       - The function has no data dependency on its input
#
#   - Some function don't need a backwards implementation because they
#     are implemented as a composition of other (differentiable) ATen
#     functions.  These are dispatched directly to the Type superclass,
#     which will in turn dispatch back to VariableType for its
#     differentiable subcomponents.
#
from __future__ import print_function
from .utils import CodeTemplate, nested_dict, write, uninplace_api_name
from .gen_autograd import VIEW_FUNCTIONS, VIEW_FUNCTIONS_WITH_METADATA_CHANGE, \
    MULTI_OUTPUT_SAFE_FUNCTIONS, RETURNS_VIEWS_OF_INPUT
from .gen_autograd_functions import uses_single_grad

# These functions we don't want to record for tracing, because we always want
# to trace their constituent parts.  This is a temporary hack in lieue
# of proper scopes, where subsequent compilation passes can ask for the unfolding
# on demand.  Only concrete ATen methods can be disabled this way; it will have
# NO EFFECT otherwise.
DONT_RECORD_TRACE = {
    'convolution', 'conv1d', 'conv2d', 'conv3d', 'conv_transpose1d',
    'conv_transpose2d', 'conv_transpose3d', 'lstm_cell', 'gru_cell',
    'rnn_tanh_cell', 'rnn_relu_cell', 'linear',
    # FIXME: figure out a better way when we support sparse tensors in jit
    '_coalesced_',
}

# These functions have their names recorded under trace renamed,
RENAME_TRACE = {
    'zero': 'zeros_like',  # replacing aten::zero_ with aten::zeros_like
    'fill': 'full_like',  # replacing aten::fill_ with aten::full_like
}

# `torch.jit.trace` have undocumented keyword argument `_force_outplace`,
# which force jit to replace functions with outplace variants (for
# example `aten::add_` becomes `aten::add`).
#
# This replacement implemented in-place with minimum modifications of
# arguments stack (as it assumes that outplace call has the same arguments
# as inplace version).
#
# However there are no such substitutions available for `aten::fill_`
# and `aten::zero_` operators, as we never implemented `aten::fill`
# and `aten::zero`. So jit tracing hack replacing `aten::zero_` with
# `aten::zeros_like` and replacing `aten::fill_` with `aten::full_like`.
#
# But as they potentially can have different arguments, we also have
# to hack into the stack and add missing ones.
#
# A possible alternative would be:
#
#  - Add `aten::fill` and `aten::zero`
#
#  - Or keep `aten::zeros_like` arguments aligned with `aten::zero_`
# arguments (inside of the `native_functions.yaml`)
RENAME_TRACE_ADD_ARGS = {
    'fill': '''\
    jit::tracer::addInputs(node, "options", TensorOptions());
    c10::optional<MemoryFormat> memory_format = c10::MemoryFormat::Preserve;
    jit::tracer::addInputs(node, "memory_format", memory_format);
''',
    'zero': '''\
    jit::tracer::addInputs(node, "options", TensorOptions());
    c10::optional<MemoryFormat> memory_format = c10::MemoryFormat::Preserve;
    jit::tracer::addInputs(node, "memory_format", memory_format);
''',
}

# (declaration name, argument name) -> attribute name
RENAME_ATTRIBUTES = {
    ('fill_', 'value'): 'fill_value'
}

# These functions are not worth profiling because they are very cheap and may
# be called very often.
DONT_PROFILE = {
    'data_ptr', 'get_device', 'is_contiguous', 'is_cuda', 'is_distributed',
    'is_same_size', 'is_set_to', 'is_signed', 'is_sparse', 'numel',
    'size', 'storage_offset', 'stride',
}

# We don't set or modify grad_fn on these methods. Generally, they return
# tensors that have requires_grad=False. In-place functions listed here will
# not examine or modify requires_grad or grad_fn.
DONT_REQUIRE_DERIVATIVE = {
    # These only depend on the input Tensor's shape and device, not the data
    'ones_like', 'zeros_like', 'rand_like', 'randn_like',
    # These are only implemented on integral types
    '__and__', '__iand__', '__ilshift__', '__ior__', '__irshift__', '__ixor__',
    '__lshift__', '__or__', '__rshift__', '__xor__',
    # These work on integral data types, and hence don't require derivative
    '_sobol_engine_draw', '_sobol_engine_ff', '_sobol_engine_scramble_',
    '_sobol_engine_initialize_state_',
    # This is an unsafe method that is meant to be out of reach of autograd.
    '_coalesced_',
    # Quantize functions should not record gradients
    'quantize_per_tensor', 'quantize_per_channel',
    # Functions that return integers should not have output that require gradients
    'argmax', 'argmin', 'argsort',
}

# Some operators invalidate the grad_accumulator. Let's reset it.
RESET_GRAD_ACCUMULATOR = {
    'set', 'resize'
}

# NOTE [ Invariant: TensorImpl and Storage Pointer Equality ]
#
# When a function modifies its input tensors (via inplace or out-variants),
# it should never change the the input tensors' underlying c10::TensorImpl pointers
# or c10::Storage pointers.
#
# The following code templates implement the checks for this invariant:
SAVE_TENSOR_STORAGE = CodeTemplate("""\
c10::optional<Storage> ${tensor_name}_storage_saved =
  ${tensor_name}.has_storage() ? c10::optional<Storage>(${tensor_name}.storage()) : c10::nullopt;
""")

ENFORCE_SAME_TENSOR_STORAGE = CodeTemplate("""\
if (${tensor_name}_storage_saved.has_value())
  AT_ASSERT(${tensor_name}_storage_saved.value().is_alias_of(${tensor_name}.storage()));
""")

SAVE_TENSORLIST_STORAGE = CodeTemplate("""\
std::vector<c10::optional<Storage>> ${tensorlist_name}_storage_saved(${tensorlist_name}.size());
for (const Tensor& tensor : ${tensorlist_name})
  ${tensorlist_name}_storage_saved.push_back(
    tensor.has_storage() ? c10::optional<Storage>(tensor.storage()) : c10::nullopt);
""")

ENFORCE_SAME_TENSORLIST_STORAGE = CodeTemplate("""\
for (size_t i=0; i<${tensorlist_name}.size(); i++) {
  if (${tensorlist_name}_storage_saved[i].has_value())
    AT_ASSERT(${tensorlist_name}_storage_saved[i].value().is_alias_of(${tensorlist_name}[i].storage()));
}
""")

SAVE_TENSOR_IMPL = CodeTemplate("""\
c10::intrusive_ptr<TensorImpl> ${tensor_name}_impl_saved;
if (${tensor_name}.defined()) ${tensor_name}_impl_saved = ${tensor_name}.getIntrusivePtr();
""")

ENFORCE_SAME_TENSOR_IMPL = CodeTemplate("""\
if (${tensor_name}_impl_saved) AT_ASSERT(${tensor_name}_impl_saved == ${tensor_name}.getIntrusivePtr());
""")

SAVE_TENSORLIST_IMPL = CodeTemplate("""\
std::vector<c10::intrusive_ptr<TensorImpl>> ${tensorlist_name}_impl_saved(${tensorlist_name}.size());
for (size_t i=0; i<${tensorlist_name}.size(); i++)
  if (${tensorlist_name}[i].defined()) ${tensorlist_name}_impl_saved[i] = ${tensorlist_name}[i].getIntrusivePtr();
""")

ENFORCE_SAME_TENSORLIST_IMPL = CodeTemplate("""\
for (size_t i=0; i<${tensorlist_name}.size(); i++) {
  if (${tensorlist_name}_impl_saved[i])
    AT_ASSERT(${tensorlist_name}_impl_saved[i] == ${tensorlist_name}[i].getIntrusivePtr());
}
""")

# The following list contains functions that we don't enforce the invariant on.
DONT_ENFORCE_SAME_TENSOR_IMPL_OR_STORAGE = {
    # These functions are expected to change impl or storage of input tensors
    'set_', '_cudnn_rnn_flatten_weight',
}
# END CHECKS FOR [ Invariant: TensorImpl and Storage Pointer Equality ]

METHOD_DECLARATION = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) ;
""")

METHOD_DEFINITION = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) {
  ${type_definition_body}
}
""")

# See NOTE[UnboxedOnly] in function_wrapper.py
UNBOXEDONLY_WRAPPER_REGISTRATION = CodeTemplate("""\
m.impl_UNBOXED("${unqual_operator_name_with_overload}", &${class_type}::${type_wrapper_name});
""")

WRAPPER_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       c10::impl::hacky_wrapper_for_legacy_signatures(TORCH_FN(${class_type}::${type_wrapper_name}))
);
""")

UNPACK_TENSOR = CodeTemplate("""\
auto${ref} ${arg_name}_ = unpack${suffix}(${arg_name}, "${arg_name}", ${arg_pos});""")

UNPACK_OPTIONS = CodeTemplate("""\
auto ${arg_name}_ = TensorOptions(${arg_name});""")

DECLARE_GRAD_FN = CodeTemplate("""\
std::shared_ptr<${op}> grad_fn;
""")

SETUP_DERIVATIVE = CodeTemplate("""\
if (compute_requires_grad( ${args_with_derivatives} )) {
  ${setup}
}
""")

ASSIGN_GRAD_FN = CodeTemplate("""\
grad_fn = std::shared_ptr<${op}>(new ${op}(${op_ctor}), deleteNode);
grad_fn->set_next_edges(collect_next_edges( ${args_with_derivatives} ));
""")

CALL_DEFAULT = CodeTemplate("""\
TypeDefault::${type_wrapper_name}(${args})""")

CALL_DISPATCH_VIA_NAMESPACE = CodeTemplate("""\
at::${api_name}(${unpacked_args})""")

CALL_DISPATCH_VIA_METHOD = CodeTemplate("""\
${var}.${api_name}(${unpacked_method_args})""")

# If the non-variable operation has return values, we use the `tmp` variable to hold the
# values temporarily and pass the values to the return variables outside of the
# `at::AutoNonVariableTypeMode` guard block.
DISPATCH_TO_NON_VAR_TYPE_WITH_TMP_RETURN_VALUES = CodeTemplate("""\
auto tmp = ([&]() {
  at::AutoNonVariableTypeMode non_var_type_mode(true);
  return ${base_type_call};
})();
""")

ASSIGN_RETURN_VALUE = CodeTemplate("""\
${return_values} = ${rhs_value};
""")

ARRAYREF_TO_VEC = CodeTemplate("""\
auto ${vec} = ${arg}.vec();
""")

OPTIONAL_TO_VAL = CodeTemplate("""\
auto ${val} = ${arg}.value_or(${default});
""")

SETUP_REPLAY_VIEW_IF_NOT_SUPPORT_AS_STRIDED_OR_VIEW_WITH_METADATA_CHANGE = CodeTemplate("""\
c10::optional<std::function<at::Tensor(const at::Tensor&)>> func=c10::nullopt;
if (${is_view_with_metadata_change} || !self.unsafeGetTensorImpl()->support_as_strided()) {
  ${replay_view_func}
}
""")

REPLAY_VIEW_LAMBDA_FUNC = CodeTemplate("""\
func = [=](const at::Tensor& ${input_base}) {
  return ${replay_view_call};
};
""")

DISPATCH_TO_NON_VAR_TYPE_WITHOUT_RETURN_VALUES = CodeTemplate("""\
{
  at::AutoNonVariableTypeMode non_var_type_mode(true);
  ${base_type_call};
}
""")

SET_HISTORY = CodeTemplate("""\
if (grad_fn) {
    ${fn}_history(${differentiable_outputs}, grad_fn);
}
""")

CONDITIONAL = CodeTemplate("""\
if (${cond}) {
  ${statements}
}
""")

RECORD_FUNCTION = CodeTemplate("""\
RECORD_FUNCTION("${name}", std::vector<c10::IValue>({${input_names}}), Node::peek_at_next_sequence_nr());
""")

SELECT = CodeTemplate("""\

if (${cond}) {
  ${true}
} else {
  ${false}
}
""")

OP_NAME = CodeTemplate("""\
op_name = jit::Symbol::fromQualString("aten::${trace_name}");
""")

PRE_RECORD_TRACE = CodeTemplate("""\
torch::jit::Node* node = nullptr;
std::shared_ptr<jit::tracer::TracingState> tracer_state;
if (jit::tracer::isTracing()) {
  tracer_state = jit::tracer::getTracingState();
  at::Symbol op_name;
  ${set_op_name}
  node = tracer_state->graph->create(op_name, /*num_outputs=*/0);
  jit::tracer::recordSourceLocation(node);
  ${add_trace_inputs}
  tracer_state->graph->insertNode(node);
  ${inplace_guard}
  jit::tracer::setTracingState(nullptr);
}
""")

INPLACE_GUARD = CodeTemplate("""\
jit::tracer::ensureUniqueIfOutOfPlaced("${name}", ${mutable_input});
""")

ADD_TRACE_INPUT = CodeTemplate("""jit::tracer::addInputs(node, "${name}", ${input});""")

POST_RECORD_TRACE = CodeTemplate("""\
if (tracer_state) {
  jit::tracer::setTracingState(std::move(tracer_state));
  ${add_trace_outputs}
}
""")

RUN_ONLY_IN_DEBUG_MODE = CodeTemplate("""\
#ifndef NDEBUG
${statements}
#endif
""")

# Generate a file that lists all functions and their schema string. Used for XLA
REGISTRATION_DECLARATION = CodeTemplate("""\
${return_type} ${api_name}(${declaration_formals}); // {"schema": "${schema_string}", "compound": "${compound}"}
""")

# ProfiledType templates
# See NOTE[UnboxedOnly] in function_wrapper.py
UNBOXED_PROFILE_DISPATCH = CodeTemplate("""\
static auto op = c10::Dispatcher::singleton()
    .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
    .typed<${return_type} (${profiled_arg_types})>();
RECORD_FUNCTION("${name}", std::vector<c10::IValue>({${input_names}}), Node::peek_at_next_sequence_nr());
return c10::Dispatcher::singleton().redispatch<${profiled_ret_and_arg_types}>(${profiled_dispatch_args});
""")
PROFILE_DISPATCH = CodeTemplate("""\
static auto op = c10::Dispatcher::singleton()
    .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
    .typed<${return_type} (${profiled_arg_types})>();
RECORD_FUNCTION("${name}", std::vector<c10::IValue>({${input_names}}), Node::peek_at_next_sequence_nr());
return c10::Dispatcher::singleton().redispatch<${profiled_ret_and_arg_types}>(${profiled_dispatch_args});
""")


# TraceType templates
# TODO: change `redispatch` to `NoTracerDispatchMode` + regular `call`.
# See NOTE[UnboxedOnly] in function_wrapper.py
UNBOXED_TRACE_DISPATCH = CodeTemplate("""\
static auto op = c10::Dispatcher::singleton()
    .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
    .typed<${return_type} (${arg_types})>();
${assign_return_values}c10::Dispatcher::singleton().redispatch<${ret_and_arg_types}>(${trace_dispatch_args});
""")
TRACE_DISPATCH = CodeTemplate("""\
static auto op = c10::Dispatcher::singleton()
    .findSchemaOrThrow("aten::${operator_name}", "${overload_name}")
    .typed<${return_type} (${schema_order_arg_types})>();
${assign_return_values}c10::Dispatcher::singleton()
    .redispatch<${schema_order_ret_and_arg_types}>(${schema_order_trace_dispatch_args});
""")


FACTORY_FUNCTION_NAMES = None


def find_factory_functions(declarations):
    global FACTORY_FUNCTION_NAMES
    FACTORY_FUNCTION_NAMES = set()

    for declaration in declarations:
        if declaration['is_factory_method']:
            FACTORY_FUNCTION_NAMES.add(declaration['api_name'])


def should_trace(declaration):
    # Operations involving Storage or Type are not traceable at the moment
    if any(arg['simple_type'] in {'Storage', 'Type', 'ConstQuantizerPtr'} for arg in declaration['arguments']):
        return False
    # We can't trace functions which don't have any Tensor or TensorList returns
    if 'Tensor' not in declaration['return_type']:
        return False
    name = declaration['name']
    base_name = name[:-1] if declaration['inplace'] else name[:-4] if name.endswith('_out') else name
    if base_name in DONT_RECORD_TRACE or name in DONT_RECORD_TRACE:
        return False
    return True


def is_out_overload(declaration):
    return declaration['api_name'].endswith('_out')


def format_postrecord_trace(declaration):
    # For outplacing ops, *_out overloads require special handling to move the
    # output *argument* to a return value
    if is_out_overload(declaration):
        output_names_outplace = [arg['name'] for arg in declaration['arguments'] if arg.get('output', False)]
        output_names_inplace = [r['name'] for r in declaration['returns']]

        # Code size optimization: the common case is that the return value is
        # the same for both variants
        if output_names_outplace == output_names_inplace:
            outputs = ['jit::tracer::addOutput(node, {});'.format(n) for n in output_names_outplace]
            return POST_RECORD_TRACE.substitute(add_trace_outputs=outputs)

        local = {}
        local['cond'] = 'force_outplace'
        local['true'] = ['jit::tracer::addOutput(node, {});'.format(n) for n in output_names_outplace]
        local['false'] = ['jit::tracer::addOutput(node, {});'.format(n) for n in output_names_inplace]
        selection = SELECT.substitute(local)
        return POST_RECORD_TRACE.substitute(add_trace_outputs=selection)

    output_names = [r['name'] for r in declaration['returns']]
    outputs = ['jit::tracer::addOutput(node, {});'.format(n) for n in output_names]
    return POST_RECORD_TRACE.substitute(add_trace_outputs=outputs)


def format_trace_op_name(declaration):
    is_inplace = declaration['api_name'] != uninplace_api_name(declaration['api_name'])

    if not is_inplace or is_out_overload(declaration):
        # special case for *_out functions: the in-place and out-of-place ops
        # are overloaded with the same name in the JIT
        trace_name = uninplace_api_name(declaration['api_name'])
        trace_name = RENAME_TRACE.get(trace_name, trace_name)
        return OP_NAME.substitute(trace_name=trace_name)

    # otherwise, this is an in-place op and we need to emit both in- and
    # out-of-place versions
    outplace_trace_name = uninplace_api_name(declaration['api_name'])
    inplace_trace_name = declaration['api_name']
    outplace_trace_name = RENAME_TRACE.get(outplace_trace_name, outplace_trace_name)
    inplace_trace_name = RENAME_TRACE.get(inplace_trace_name, inplace_trace_name)

    select_params = {}
    select_params['cond'] = 'tracer_state->force_outplace'
    select_params['true'] = OP_NAME.substitute(trace_name=outplace_trace_name)
    select_params['false'] = OP_NAME.substitute(trace_name=inplace_trace_name)

    return SELECT.substitute(select_params)


def format_trace_inputs(declaration):
    def dispatch_trace_input(arg_spec):
        name, value, simple_type, nullable = arg_spec
        # XXX: For arg that have type of Tensor?[], tracer will pass allow_undefined to addInputs
        if simple_type == 'TensorList' and nullable:
            return '''jit::tracer::addInputs(node, "{}", {}, {});'''.format(name, value, "true")
        else:
            return ADD_TRACE_INPUT.substitute(name=name, input=value)

    trace_inputs = declaration['arguments']

    if is_out_overload(declaration):
        # *_out functions take the result as a first argument, but they are the
        # last argument in the JIT schema.
        out_input = trace_inputs[0]
        trace_inputs = trace_inputs[1:]

    trace_input_spec = [(i['name'], i['name'], i['simple_type'], i.get('is_nullable')) for i in trace_inputs]

    trace_inputs = \
        '\n'.join(dispatch_trace_input(arg_spec) for arg_spec in trace_input_spec)

    if is_out_overload(declaration):
        # for *_out functions, handle the result argument differently for inplace/outplace.
        # For inplace: just add the input to the end to confirm with the JIT schema
        inplace = ADD_TRACE_INPUT.substitute(name=out_input['name'], input=out_input['name'])

        # for outplace: do nothing, except if the declaration is a factory.
        # Factories are a bit special because their out-of-place overloads
        # take an extra TensorOptions argument, which is missing in the _out function
        trace_name = uninplace_api_name(declaration['api_name'])
        has_factory_name = trace_name in FACTORY_FUNCTION_NAMES
        if has_factory_name:
            outplace = ADD_TRACE_INPUT.substitute(name='out', input='out.options()')
        else:
            outplace = ''

        trace_inputs += '\n'
        trace_inputs += SELECT.substitute(
            cond='tracer_state->force_outplace', true=outplace, false=inplace)

    return trace_inputs


def format_prerecord_trace(declaration):
    local = {}
    is_inplace = declaration['api_name'] != uninplace_api_name(declaration['api_name'])

    local['set_op_name'] = format_trace_op_name(declaration)

    is_inplace = declaration['api_name'] != uninplace_api_name(declaration['api_name'])
    add_args = ''
    if is_inplace:
        api_name = uninplace_api_name(declaration['api_name'])
        add_args = RENAME_TRACE_ADD_ARGS.get(api_name, '')
    if add_args:
        select_params = {}
        select_params['cond'] = 'tracer_state->force_outplace'
        select_params['true'] = add_args
        select_params['false'] = ''
        additional_inputs = SELECT.substitute(select_params)
    else:
        additional_inputs = ''
    local['add_trace_inputs'] = format_trace_inputs(declaration) + additional_inputs

    local['inplace_guard'] = ''
    if is_inplace:
        local['inplace_guard'] = INPLACE_GUARD.substitute(
            name=declaration['api_name'],
            mutable_input=declaration['arguments'][0]['name'])

    return PRE_RECORD_TRACE.substitute(local)


def format_trace(declaration):
    if not should_trace(declaration):
        return ('', '')
    return (format_prerecord_trace(declaration), format_postrecord_trace(declaration))


# Methods shared by TraceType and VariableType to handle return variable declaration, tie and tuple.
def format_return_variables(declaration):
    name = declaration['name']
    arguments = declaration['arguments']
    inplace = declaration['inplace']
    is_out_fn = name.endswith('_out')
    modifies_arguments = inplace or is_out_fn

    def declare_returned_variables():
        if modifies_arguments:
            return ''
        if len(declaration['returns']) == 1:
            return ''
        # TODO: this will be ugly
        names = [ret['type'] + ' ' + ret['name'] + ';' for ret in declaration['returns']]
        return '\n'.join(names)

    def tie_return_values():
        if len(declaration['returns']) == 1:
            return 'auto {}'.format(declaration['returns'][0]['name'])
        names = [ret['name'] for ret in declaration['returns']]
        return 'std::tie({})'.format(', '.join(names))

    def get_return_value():
        if inplace:
            return 'self'
        if is_out_fn:
            return_names = [arg['name'] for arg in arguments
                            if arg.get('output', False)]
            if len(return_names) == 1:
                return return_names[0]
            return 'std::forward_as_tuple({})'.format(', '.join(return_names))

        returns = declaration['returns']
        if len(returns) == 1:
            return returns[0]['name']
        moved = ['std::move({})'.format(r['name']) for r in returns]
        return 'std::make_tuple({})'.format(', '.join(moved))

    return (declare_returned_variables(), tie_return_values(), get_return_value())


def gen_variable_type(out, aten_declarations, template_path):

    """VariableType.h and VariableType.cpp body

    This is the at::Type subclass for differentiable tensors. The
    implementation of each function dispatches to the base tensor type to
    compute the output. The grad_fn is attached to differentiable functions.
    """

    # WARNING: this function call modifies global mutable state
    find_factory_functions(aten_declarations)

    aten_declarations = list(sorted(aten_declarations, key=lambda decl: decl['name']))

    gen_variable_type_shard(out, aten_declarations, template_path, None, True)

    # NOTE: see Note [Sharded File] at the top of the VariableType.cpp
    # template regarding sharding of the generated files.
    num_shards = 5
    shards = [[] for _ in range(num_shards)]

    # functions are assigned arbitrarily but stably to a file based on hash
    for decl in aten_declarations:
        x = sum(ord(c) for c in decl['name']) % num_shards
        shards[x].append(decl)

    for i, shard in enumerate(shards):
        gen_variable_type_shard(out, shard, template_path, '_%d' % i, False)
    gen_variable_type_shard(out, aten_declarations, template_path, 'Everything', False)

    REGISTRATION_DECLARATIONS_H = CodeTemplate.from_file(template_path + "/RegistrationDeclarations.h")
    registration_declarations = []

    for declaration in aten_declarations:
        if declaration['use_c10_dispatcher'] == 'full':
            declaration_formals = declaration['schema_order_formals']
        else:
            assert declaration['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
            declaration_formals = declaration['formals']
        if dispatch_strategy(declaration) == 'use_derived':
            registration_declarations.append(
                REGISTRATION_DECLARATION.substitute(declaration,
                                                    declaration_formals=declaration_formals,
                                                    compound='false'))
        else:
            registration_declarations.append(
                REGISTRATION_DECLARATION.substitute(declaration,
                                                    declaration_formals=declaration_formals,
                                                    compound='true'))

    env = {
        'registration_declarations': registration_declarations,
    }
    write(out, 'RegistrationDeclarations.h', REGISTRATION_DECLARATIONS_H, env)


def gen_variable_type_shard(out, aten_declarations, template_path, suffix, header):
    VARIABLE_TYPE_H = CodeTemplate.from_file(template_path + '/VariableType.h')
    VARIABLE_TYPE_CPP = CodeTemplate.from_file(template_path + '/VariableType.cpp')
    PROFILED_TYPE_CPP = CodeTemplate.from_file(template_path + '/ProfiledType.cpp')
    TRACE_TYPE_CPP = CodeTemplate.from_file(template_path + '/TraceType.cpp')

    type_declarations = []
    type_definitions = []
    wrapper_registrations = []
    profiled_method_definitions = []
    profiled_wrapper_registrations = []
    trace_method_definitions = []
    trace_wrapper_registrations = []

    for declaration in aten_declarations:
        formal_types = [arg['type'] for arg in declaration['arguments']]
        type_declarations.append(METHOD_DECLARATION.substitute(declaration))
        if not declaration['manual_kernel_registration']:
            body = emit_body(declaration)
            type_definitions.append(METHOD_DEFINITION.substitute(
                declaration, type_definition_body=body))
            if declaration['use_c10_dispatcher'] == 'full':
                wrapper_registrations.append(WRAPPER_REGISTRATION.substitute(
                    declaration, class_type='VariableType'))
            else:
                assert declaration['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
                wrapper_registrations.append(UNBOXEDONLY_WRAPPER_REGISTRATION.substitute(
                    declaration, class_type='VariableType'))

        # Emit ProfiledType code
        profiled_body = emit_profiled_body(declaration)
        profiled_method_definitions.append(METHOD_DEFINITION.substitute(
            declaration, type_definition_body=profiled_body))

        if declaration['use_c10_dispatcher'] == 'full':
            profiled_wrapper_registrations.append(WRAPPER_REGISTRATION.substitute(
                declaration, class_type='ProfiledType'))
        else:
            assert declaration['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
            profiled_wrapper_registrations.append(UNBOXEDONLY_WRAPPER_REGISTRATION.substitute(
                declaration, class_type='ProfiledType'))

        # Emit TraceType code
        if not declaration['manual_kernel_registration']:
            trace_body = emit_trace_body(declaration)
            trace_method_definitions.append(METHOD_DEFINITION.substitute(
                declaration, type_definition_body=trace_body))

            if declaration['use_c10_dispatcher'] == 'full':
                trace_wrapper_registrations.append(WRAPPER_REGISTRATION.substitute(
                    declaration, class_type='TraceType'))
            else:
                trace_wrapper_registrations.append(UNBOXEDONLY_WRAPPER_REGISTRATION.substitute(
                    declaration, class_type='TraceType'))

    env = {
        'type_derived_method_declarations': type_declarations,
        'type_derived_method_definitions': type_definitions,
        'wrapper_registrations': wrapper_registrations,
        'profiled_method_definitions': profiled_method_definitions,
        'profiled_wrapper_registrations': profiled_wrapper_registrations,
        'trace_method_definitions': trace_method_definitions,
        'trace_wrapper_registrations': trace_wrapper_registrations,
    }
    if header:
        write(out, 'VariableType.h', VARIABLE_TYPE_H, env)
    else:
        write(out, 'VariableType%s.cpp' % suffix, VARIABLE_TYPE_CPP, env)
        write(out, 'ProfiledType%s.cpp' % suffix, PROFILED_TYPE_CPP, env)
        write(out, 'TraceType%s.cpp' % suffix, TRACE_TYPE_CPP, env)


def emit_profiled_body(declaration):
    arguments = declaration['arguments']
    returns = declaration['returns']
    func = declaration['derivative']
    name = declaration['name']
    inplace = declaration['inplace']
    is_out_fn = name.endswith('_out')
    modifies_arguments = inplace or is_out_fn
    returns_void = len(returns) == 0

    processed_args = []
    for a in arguments:
        processed_args.append('{}'.format(a['name']))

    arg_types = ', '.join([a['type'] for a in declaration['arguments']])
    ret_and_arg_types = ', '.join([declaration['return_type']] + [a['type'] for a in declaration['arguments']])
    schema_order_arg_types = ', '.join([a['type'] for a in declaration['schema_order_arguments']])
    schema_order_ret_and_arg_types = ', '.join(
        [declaration['return_type']] + [a['type'] for a in declaration['schema_order_arguments']])

    def check_record_function_input_type(simple_type):
        return simple_type in ['Tensor', 'Scalar']

    def record_function_input_names():
        return ', '.join([
            arg['name'] for arg in declaration['arguments']
            if check_record_function_input_type(arg['simple_type'])])

    profiled_dispatch_args = ['op', 'c10::DispatchKey::Profiler'] + declaration['args']
    schema_order_profiled_dispatch_args = ['op', 'c10::DispatchKey::Profiler'] + declaration['schema_order_args']

    if declaration['use_c10_dispatcher'] == 'full':
        profiled_arg_types = schema_order_arg_types
        profiled_ret_and_arg_types = schema_order_ret_and_arg_types
        profiled_dispatch_args = schema_order_profiled_dispatch_args
    else:
        assert declaration['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
        profiled_arg_types = arg_types
        profiled_ret_and_arg_types = ret_and_arg_types
        profiled_dispatch_args = profiled_dispatch_args

    call = PROFILE_DISPATCH.substitute(
        declaration,
        name=name,
        input_names=record_function_input_names(),
        return_type=declaration['return_type'],
        profiled_arg_types=profiled_arg_types,
        profiled_ret_and_arg_types=profiled_ret_and_arg_types,
        profiled_dispatch_args=profiled_dispatch_args,
    )

    return [call]


def emit_trace_body(declaration):
    returns = declaration['returns']
    name = declaration['name']
    inplace = declaration['inplace']
    is_out_fn = name.endswith('_out')
    modifies_arguments = inplace or is_out_fn
    returns_void = len(returns) == 0

    trace_body = []
    pre_record_trace, post_record_trace = format_trace(declaration)
    declare_returned_variables, tie_return_values, get_return_value = format_return_variables(declaration)

    trace_body.append(pre_record_trace)
    trace_body.append(declare_returned_variables)

    arg_types = ', '.join([a['type'] for a in declaration['arguments']])
    ret_and_arg_types = ', '.join([declaration['return_type']] + [a['type'] for a in declaration['arguments']])
    schema_order_arg_types = ', '.join([a['type'] for a in declaration['schema_order_arguments']])
    schema_order_ret_and_arg_types = ', '.join(
        [declaration['return_type']] + [a['type'] for a in declaration['schema_order_arguments']])

    trace_dispatch_args = ['op', 'c10::DispatchKey::Tracer'] + declaration['args']
    schema_order_trace_dispatch_args = ['op', 'c10::DispatchKey::Tracer'] + declaration['schema_order_args']
    assign_return_values = '{} = '.format(tie_return_values) if not modifies_arguments and not returns_void else ''
    if declaration['use_c10_dispatcher'] == 'full':
        call = TRACE_DISPATCH.substitute(
            declaration,
            schema_order_arg_types=schema_order_arg_types,
            assign_return_values=assign_return_values,
            schema_order_ret_and_arg_types=schema_order_ret_and_arg_types,
            schema_order_trace_dispatch_args=schema_order_trace_dispatch_args,
        )
    else:
        assert declaration['use_c10_dispatcher'] == 'with_codegenerated_unboxing_wrapper'
        call = UNBOXED_TRACE_DISPATCH.substitute(
            declaration,
            arg_types=arg_types,
            ret_and_arg_types=ret_and_arg_types,
            trace_dispatch_args=trace_dispatch_args,
            assign_return_values=assign_return_values,
        )
    trace_body.append(call)
    trace_body.append(post_record_trace)
    if not returns_void:
        trace_body.append('return {};'.format(get_return_value))
    return trace_body


def emit_body(declaration):
    strategy = dispatch_strategy(declaration)

    arguments = declaration['arguments']
    returns = declaration['returns']
    func = declaration['derivative']
    name = declaration['name']
    inplace = declaration['inplace']
    is_out_fn = name.endswith('_out')
    modifies_arguments = inplace or is_out_fn
    returns_void = len(returns) == 0

    base_name = name[:-1] if inplace else name[:-4] if is_out_fn else name
    view_info = VIEW_FUNCTIONS.get(base_name, None)
    if view_info is None and base_name in RETURNS_VIEWS_OF_INPUT:
        view_info = "self"

    def is_differentiable(arg):
        if 'TensorOptions' in arg['type']:
            return False
        if 'Tensor' not in arg['type']:
            return False
        if arg['name'] in declaration.get('non_differentiable_arg_names', []):
            return False
        return True

    def find_args_with_derivatives(differentiable_inputs):
        """Find arguments that have derivative definitions"""
        if func is None:
            return differentiable_inputs
        names = set(name for d in func['derivatives'] for name in d['var_names'])
        differentiable = [arg for arg in differentiable_inputs if arg['name'] in names]
        if len(differentiable) != len(names):
            missing = names - set(arg['name'] for arg in differentiable)
            raise RuntimeError('Missing arguments for derivatives: {} in {}'.format(missing, func['name']))
        return differentiable

    inputs = [arg for arg in arguments if not arg.get('output', False)]
    differentiable_inputs = list(filter(is_differentiable, inputs))
    args_with_derivatives = find_args_with_derivatives(differentiable_inputs)
    non_differentiable_arg_names = declaration.get('non_differentiable_arg_names', [])
    candidate_differentiable_outputs = list(filter(is_differentiable, returns))

    if declaration['output_differentiability'] is not None:
        differentiable_outputs = []
        output_differentiability = declaration['output_differentiability']
        if False in output_differentiability and inplace:
            raise RuntimeError("output_differentiability=False for inplace operation (version_counter won't get updated)")
        for differentiable, output in zip(output_differentiability, returns):
            if differentiable:
                differentiable_outputs.append(output)
    elif uses_single_grad(func):
        differentiable_outputs = candidate_differentiable_outputs[:1]
    else:
        differentiable_outputs = candidate_differentiable_outputs

    requires_derivative = (
        base_name not in DONT_REQUIRE_DERIVATIVE and name not in DONT_REQUIRE_DERIVATIVE and
        len(differentiable_inputs) > 0 and len(differentiable_outputs) > 0 and
        strategy == 'use_derived')

    if func is not None and not requires_derivative:
        raise RuntimeError('ERROR: derivative ignored for {} -- specified an autograd function without derivative'
                           .format(name))

    def emit_save_inputs():
        setup = []
        if func is None:
            return setup

        has_tensorlist_arg = any(arg['type'] == 'TensorList' for arg in func['args_with_derivatives'])

        # We don't want to save tensors if we know that they will never be used
        # when computing the derivative, so we add guards to those statements
        def guard_for(arg):
            # It's hard to determine the edge offset if we have TensorLists
            if has_tensorlist_arg:
                return None

            # Empirical evaluation of the cases where we insert those guards in
            # backward show that they are somewhat useless. E.g. there's no need
            # to guard on some values captured from forward, because they had to
            # require_grad if the backward function even gets executed. I don't
            # have any good ideas for detecting those cases, so I simply disabled the
            # checks.
            if 'backward' in func['name']:
                return None

            # If there's a single derivative we could compute, we already have
            # a requires_grad check that is sufficient
            if len(func['args_with_derivatives']) <= 1:
                return None

            # We really only care about trimming down the amount of tensors we save
            if arg['type'] != 'Tensor':
                return None

            # We want to emit simple guards, so we only allow that if checking one
            # input is enough to determine whether we need that value
            used_in = [d for d in func['derivatives'] if arg in d['saved_inputs']]
            assert len(used_in) > 0
            if len(used_in) != 1:
                return None
            derivative = used_in[0]
            if len(derivative['var_names']) != 1:
                return None
            derivative_var_name = derivative['var_names'][0]

            # Figure out the offset of the edge that uses this variable
            for edge_off, arg in enumerate(func['args_with_derivatives']):
                if arg['name'] == derivative_var_name:
                    break
            else:
                raise AssertionError()

            return 'grad_fn->should_compute_output({})'.format(edge_off)

        setup.extend(save_variables(func['saved_inputs'], False, guard_for))
        for arg in func['args_with_derivatives']:
            if arg['type'] == 'TensorList':
                setup.append("grad_fn->{}_size_ = {}.size();".format(arg['name'], arg['name']))

        return setup

    def setup_derivative(differentiable_inputs):

        env = {}
        env['args_with_derivatives'] = [arg['name'] for arg in args_with_derivatives]
        env['op'] = func['op'] if func is not None else 'NotImplemented'
        env['op_ctor'] = '' if func is not None else '"{}"'.format(declaration['api_name'])

        if is_out_fn:
            setup = ['throw_error_out_requires_grad("{}");'.format(base_name)]
            body = []
            body.append(DECLARE_GRAD_FN.substitute(op='Node'))
            body.append(SETUP_DERIVATIVE.substitute(
                setup=setup,
                args_with_derivatives=[arg['name'] for arg in differentiable_inputs]))
            body.append(SETUP_DERIVATIVE.substitute(
                setup=setup,
                args_with_derivatives=[arg['name'] for arg in differentiable_outputs]))
            return body

        setup = []
        setup.extend(ASSIGN_GRAD_FN.substitute(env).split('\n'))
        setup.extend(emit_save_inputs())

        body = []
        body.extend(emit_check_no_requires_grad(differentiable_inputs, args_with_derivatives))
        body.append(DECLARE_GRAD_FN.substitute(env))
        body.append(SETUP_DERIVATIVE.substitute(env, setup=setup))
        return body

    def emit_check_no_requires_grad(tensor_args, args_with_derivatives):
        """Checks that arguments without derivatives don't require grad"""
        body = []
        for arg in tensor_args:
            if arg in args_with_derivatives:
                continue
            name = arg['name']
            if name in non_differentiable_arg_names:
                continue
            if name == 'output':
                # Double-backwards definitions sometimes take in 'input' and
                # 'output', but only define the derivative for input.
                continue
            if arg['dynamic_type'] in {'IndexTensor', 'ByteTensor', 'BoolTensor'}:
                continue
            body.append('check_no_requires_grad({}, "{}");'.format(name, name))
        return body

    def save_variables(saved_variables, is_output, guard_for=lambda name: None):
        # assign the saved variables to the generated grad_fn
        stmts = []
        for arg in saved_variables:
            name = arg['name']
            expr = arg.get('expr', arg['name'])
            if arg['type'] == 'Tensor' or (is_output and arg['type'] == 'Scalar'):
                name += '_'
                var = arg['name']
                if var == 'self' and inplace:
                    var = 'self.clone()'
                    assert not is_output
                if inplace and is_output:
                    var = 'self'
                    is_inplace_view = "{}.is_view()".format(var)
                    expr = 'SavedVariable({}, {}, {})'.format(var, str(is_output).lower(), is_inplace_view)
                else:
                    expr = 'SavedVariable({}, {})'.format(var, str(is_output).lower())
            elif arg['type'] == 'TensorList':
                name += '_'
                expr = 'make_saved_variable_list({})'.format(arg['name'])
            elif arg['type'] == 'IntArrayRef':
                expr = expr + ".vec()"
            guard = guard_for(arg)
            if guard is None:
                stmts.append('grad_fn->{} = {};'.format(name, expr))
            else:
                stmts.append('if ({}) {{'.format(guard))
                stmts.append('  grad_fn->{} = {};'.format(name, expr))
                stmts.append('}')
        return stmts

    def emit_dispatch_call(api_name, input_base, unpacked_args):
        """ Dispatch call via function in a namespace or method on Tensor."""
        if 'namespace' in declaration['method_of']:
            call = CALL_DISPATCH_VIA_NAMESPACE.substitute(
                api_name=api_name,
                unpacked_args=unpacked_args)
        else:
            call = CALL_DISPATCH_VIA_METHOD.substitute(
                api_name=api_name,
                var=input_base,
                unpacked_method_args=unpacked_args[1:])
        return call

    def emit_view_lambda():
        """ Generate an additional lambda function to recover views in backward when as_strided is not supported.
        See Note [View + Inplace update for base tensor] and [View + Inplace update for view tensor] for more details."""
        input_base = 'input_base'
        replay_view_func = ''
        updated_unpacked_args = []
        combined = nested_dict(env, declaration)
        known_view_arg_simple_types = ['int64_t', 'int64_t?', 'bool', 'IntArrayRef']
        for arg in combined['unpacked_args']:
            if arg == 'self_':
                updated_unpacked_args.append(input_base)
                continue
            arg_type = combined['unpacked_args_simple_type'][arg]
            if arg_type not in known_view_arg_simple_types:
                raise TypeError('You are adding an {} {} argument to op {} in addition to known types: {}. '
                                'Please update the list or materialize it so that it can be closed over by value, '
                                'also add a test in pytorch/xla/test/test_operations.py where this code is exercised.'
                                .format(arg_type, arg, declaration['name'], ', '.join(known_view_arg_simple_types)))

            if arg_type == 'IntArrayRef':
                # It's not safe to close over IntArrayRef by value, since this is a
                # reference type, so materialize a vector to close over by value
                arg_vec = arg + '_vec'
                replay_view_func += ARRAYREF_TO_VEC.substitute(arg=arg, vec=arg_vec)
                updated_unpacked_args.append(arg_vec)
            elif arg_type == 'int64_t?':
                # Materialize int64_t? to int64_t
                arg_value = arg + '_val'
                replay_view_func += OPTIONAL_TO_VAL.substitute(arg=arg, val=arg_value, default='0')
                updated_unpacked_args.append(arg_value)
            else:
                updated_unpacked_args.append(arg)

        replay_view_call = emit_dispatch_call(combined['api_name'], input_base, updated_unpacked_args)
        replay_view_func += REPLAY_VIEW_LAMBDA_FUNC.substitute(
            input_base=input_base,
            replay_view_call=replay_view_call)

        is_view_with_metadata_change = 'true' if name in VIEW_FUNCTIONS_WITH_METADATA_CHANGE else 'false'

        return SETUP_REPLAY_VIEW_IF_NOT_SUPPORT_AS_STRIDED_OR_VIEW_WITH_METADATA_CHANGE.substitute(
            is_view_with_metadata_change=is_view_with_metadata_change,
            replay_view_func=replay_view_func)

    def wrap_output(return_values, var):
        call = ''
        rhs_value = None
        if 'Tensor' not in declaration['return_type']:
            rhs_value = var
        elif view_info is not None:
            # See NOTE [ Autograd View Variables ] in variable.h for details.
            differentiable_output_vars = {r['name'] for r in differentiable_outputs}

            if not isinstance(view_info, str):
                raise TypeError("The view info should be a string for {}, but it is: {}".format(base_name, view_info))

            if len(differentiable_output_vars) == 0:
                # no output is differentiable (.indices() for SparseTensors for example)
                rhs_value = 'as_view({}, {}, /* is_differentiable */ false)'.format(view_info, var)
            elif len(differentiable_output_vars) == 1:
                # Single differentiable output (Tensor or Tensor[])
                return_info = differentiable_outputs[0]
                # We only support simple Tensor or a TensorList for functions that return views
                if not return_info['dynamic_type'] in ['Tensor', 'TensorList']:
                    raise RuntimeError("{} that return differentiable views can only return Tensor or Tensor[]".format(base_name))
                # Only allow rebasing of the history if we return a single Tensor
                # If we are in a no grad block, raise a warning
                # See NOTE [ View + Inplace detection ] for more details about this logic
                if return_info['dynamic_type'] == 'TensorList':
                    if base_name in MULTI_OUTPUT_SAFE_FUNCTIONS:
                        creation_meta = "CreationMeta::MULTI_OUTPUT_SAFE"
                    else:
                        creation_meta = "CreationMeta::MULTI_OUTPUT_NODE"
                    rhs_value = ("as_view(/* base */ {}, /* output */ {}, /* is_differentiable */ true, "
                                 "/* creation_meta */ {})").format(view_info, var, creation_meta)
                else:
                    call += emit_view_lambda()
                    creation_meta = "GradMode::is_enabled() ? CreationMeta::DEFAULT: CreationMeta::NO_GRAD_MODE"
                    rhs_value = ("as_view(/* base */ {}, /* output */ {}, /* is_differentiable */ true, "
                                 "/* view_func */ func, /* creation_meta */ {})").format(view_info, var, creation_meta)
            else:
                # This could be supported but we don't need it at the moment, so keeping things simple.
                raise RuntimeError("Function that return multiple differentiable output "
                                   "when at least one of them is view is not supported.")
        else:
            rhs_value = 'std::move({})'.format(var)
        assert rhs_value is not None
        call += ASSIGN_RETURN_VALUE.substitute(return_values=return_values,
                                               rhs_value=rhs_value)
        return call

    def enforce_same_tensorimpl_and_storage(env, call):
        save_ptrs_stmts = []
        enforce_same_ptrs_stmts = []
        if declaration['name'] not in DONT_ENFORCE_SAME_TENSOR_IMPL_OR_STORAGE:
            for arg in env.get('unpacked_args', []):
                simple_type = env['unpacked_args_simple_type'][arg]
                if simple_type == 'TensorList':
                    save_ptrs_stmts += [SAVE_TENSORLIST_STORAGE.substitute(tensorlist_name=arg),
                                        SAVE_TENSORLIST_IMPL.substitute(tensorlist_name=arg)]
                    enforce_same_ptrs_stmts += [ENFORCE_SAME_TENSORLIST_STORAGE.substitute(tensorlist_name=arg),
                                                ENFORCE_SAME_TENSORLIST_IMPL.substitute(tensorlist_name=arg)]
                elif simple_type == 'Tensor':
                    save_ptrs_stmts += [SAVE_TENSOR_STORAGE.substitute(tensor_name=arg),
                                        SAVE_TENSOR_IMPL.substitute(tensor_name=arg)]
                    enforce_same_ptrs_stmts += [ENFORCE_SAME_TENSOR_STORAGE.substitute(tensor_name=arg),
                                                ENFORCE_SAME_TENSOR_IMPL.substitute(tensor_name=arg)]
        assert (save_ptrs_stmts and enforce_same_ptrs_stmts) or (not save_ptrs_stmts and not enforce_same_ptrs_stmts)
        if save_ptrs_stmts and enforce_same_ptrs_stmts:
            call = RUN_ONLY_IN_DEBUG_MODE.substitute(statements=save_ptrs_stmts) + \
                call + \
                RUN_ONLY_IN_DEBUG_MODE.substitute(statements=enforce_same_ptrs_stmts)
        return call

    def emit_call(env, tie_return_values):
        combined = nested_dict(env, declaration)
        if strategy == 'use_derived':
            # We only care about adding `at::AutoNonVariableTypeMode` guard for non-variable dispatch
            # (which corresponds to 'use_derived' strategy). The purpose of this guard is to make sure
            # the baseType operations still dispatch to non-Variable type, even if the arguments passed
            # in are now Variables.
            # See NOTE [ Treating Variables as non-Variables in type dispatch ] for details.
            base_type_call = emit_dispatch_call(combined['api_name'], 'self_', combined['unpacked_args'])
            if not modifies_arguments and not returns_void:
                call = DISPATCH_TO_NON_VAR_TYPE_WITH_TMP_RETURN_VALUES.substitute(
                    base_type_call=base_type_call)

                call += wrap_output(tie_return_values, 'tmp')
            else:
                call = DISPATCH_TO_NON_VAR_TYPE_WITHOUT_RETURN_VALUES.substitute(
                    base_type_call=base_type_call)
        else:
            call = CALL_DEFAULT.substitute(declaration)
            if not modifies_arguments and not returns_void:
                call = '{} = {}'.format(tie_return_values, call)
            call = call + ';'
        call = enforce_same_tensorimpl_and_storage(env, call)
        return call

    def emit_history():
        fn = 'rebase' if modifies_arguments and view_info is None else 'set'
        output_names = [r['name'] for r in differentiable_outputs]
        # TODO: flatten allocates a std::vector, which could be expensive
        outs = CodeTemplate("flatten_tensor_args( ${outs} )").substitute(outs=output_names)
        return SET_HISTORY.substitute(fn=fn, differentiable_outputs=outs)

    def emit_save_outputs():
        if is_out_fn:
            # out functions don't currently support differentiation
            return ''
        func = declaration['derivative']
        if func is not None:
            stmts = save_variables(func['saved_outputs'], True)
            if len(stmts) == 0:
                return ''
            return CONDITIONAL.substitute(cond='grad_fn', statements=stmts)
        return ''

    def emit_check_inplace():
        if not inplace:
            return []
        return ['check_inplace({});'.format(arg['name']) for arg in differentiable_outputs]

    def emit_increment_version():
        if not modifies_arguments:
            return []
        return ['increment_version({});'.format(arg['name']) for arg in differentiable_outputs]

    env = {}
    combined = nested_dict(env, declaration)

    body = []

    declare_returned_variables, tie_return_values, get_return_value = format_return_variables(declaration)

    if strategy != 'use_type':
        body.extend(unpack_args(env, declaration))
    if requires_derivative:
        body.extend(emit_check_inplace())
        body.extend(setup_derivative(differentiable_inputs))
    body.append(declare_returned_variables)

    body.append(emit_call(env, tie_return_values))
    if requires_derivative:
        # set_flags has to appear after version_counter, because rebase_history
        # requires that the counter is incremented before it is called
        body.extend(emit_increment_version())
        body.append(emit_history())
    if requires_derivative:
        body.append(emit_save_outputs())
    if base_name in RESET_GRAD_ACCUMULATOR:
        # `inplace` implies that there is exactly one output named `self`,
        # so we can keep the generated code easy. If you need to
        # `reset_grad_accumulator` in an operator that's not `inplace`, you can
        # remove this assert but the code generation will get more elaborate
        assert inplace
        body.append('reset_grad_accumulator(self);')
    if not returns_void:
        body.append('return {};'.format(get_return_value))
    return body


def unpack_args(env, declaration):
    def requires_unpack(arg):
        return 'Tensor' in arg['dynamic_type']

    body = []
    unpacked_args = []
    unpacked_args_simple_type = {}
    for i, arg in enumerate(declaration['arguments']):
        if not requires_unpack(arg):
            unpacked_args.append(arg['name'])
            unpacked_args_simple_type[arg['name']] = arg['simple_type']
            continue

        dynamic_type = arg['dynamic_type']
        if 'TensorOptions' not in dynamic_type:
            is_nullable = arg.get('is_nullable', False)
            ref = (not is_nullable) and dynamic_type not in ['TensorList']
            suffix = '_opt' if is_nullable and dynamic_type != 'TensorList' else ''

            body.append(UNPACK_TENSOR.substitute(
                arg_name=arg['name'],
                arg_pos=i,
                suffix=suffix,
                ref='&' if ref else '',
            ))
        else:
            # Okay, we are abusing the definition of 'unpack' here a bit,
            # although it's still getting the non-variable from the variable
            # (in this case via TensorOptions rather than Variable/Tensor).
            body.append(UNPACK_OPTIONS.substitute(arg_name=arg['name']))

        unpacked_args.append(arg['name'] + '_')
        unpacked_args_simple_type[arg['name'] + '_'] = arg['simple_type']

    env['unpacked_args'] = unpacked_args
    env['unpacked_args_simple_type'] = unpacked_args_simple_type
    return body


def dispatch_strategy(declaration):
    """How are we going to call the underlying implementation of a
    declaration?  There are two strategies:

        - use_derived: we want to call the implementation on CPUDoubleType
          (or a similar, derived Type instance).  Because these derived
          instances deal in Tensors, not Variables (it's a completely different
          object, so it doesn't dispatch back to VariableType), code on
          this dispatch path needs to wrap/unwrap tensors.  If the
          derived implementation takes and returns tensors, the
          implementation is usually differentiable (although we also use
          the derived dispatch path for non-differentiable functions
          that we still want to dispatch on the derived Type instance;
          e.g., size())

        - use_type: we want to call the implementation on Type, because
          it is implemented concretely, and the functions it invokes will
          get dispatched back to VariableType (which will ensure that they
          are differentiable.)
    """
    if declaration['abstract'] or declaration['derivative'] is not None:
        # If the function is abstract (not implemented on at::Type), we must
        # call the implementation on the derived type with unpacked tensors.

        # If the function has a derivative specified and is concrete, we could
        # call either implementation. We prefer the calling the derived
        # type's implementation with unpacked tensors because it is more
        # performant in some cases: any internal calls to other ATen functions
        # won't have the history tracked.

        # If the function has a type dispatched argument (i.e. is a factory),
        # we prefer calling the derived type's implementation both because it is
        # more performant and to ensure factory functions return tensors with _version
        # of 0 (probably not strictly necessary, but nice to have to keeps versions simple
        # to understand.

        return 'use_derived'
    else:
        # If the function is concrete (we don't have to override it) and we
        # didn't declare it in derivatives.yaml, we'll assume that it is
        # actually implemented out of differentiable functions. (This
        # assumption might not hold, but then you'll see gradcheck fail.)
        return 'use_type'
