from __future__ import print_function, division, unicode_literals, absolute_import

from future import standard_library
standard_library.install_aliases()

from builtins import str, bytes
import inspect

from nipype import logging
from nipype.utils import config
from nipype.utils.misc import str2bool
from nipype.interfaces.base import (traits, DynamicTraitedSpec, Undefined, isdefined, BaseInterfaceInputSpec)
from nipype.interfaces.io import IOBase, add_traits
from nipype.utils.filemanip import filename_to_list
from nipype.utils.functions import getsource, create_function_from_source

logger = logging.getLogger('nipype.interface')

npconfig = config.NipypeConfig()

runtime_profile = str2bool(npconfig.get('execution', 'profile_runtime'))

if runtime_profile:
    try:
        import psutil
    except ImportError as exc:
        logger.info('Unable to import packages needed for runtime profiling. '\
                    'Turning off runtime profiler. Reason: %s' % exc)
        runtime_profile = False


class FunctionInputSpec(DynamicTraitedSpec, BaseInterfaceInputSpec):
    function_str = traits.Str(mandatory=True, desc='code for function')


class Function(IOBase):
    """Runs arbitrary function as an interface

    Examples
    --------

    >>> func = 'def func(arg1, arg2=5): return arg1 + arg2'
    >>> fi = Function(input_names=['arg1', 'arg2'], output_names=['out'])
    >>> fi.inputs.function_str = func
    >>> res = fi.run(arg1=1)
    >>> res.outputs.out
    6

    """

    input_spec = FunctionInputSpec
    output_spec = DynamicTraitedSpec

    def __init__(self, input_names=None, output_names='out', function=None,
                 imports=None, as_module=False, **inputs):
        """

        Parameters
        ----------

        input_names: single str or list or None
            names corresponding to function inputs
            if ``None``, derive input names from function argument names
        output_names: single str or list
            names corresponding to function outputs (default: 'out').
            if list of length > 1, has to match the number of outputs
        function : callable
            callable python object. must be able to execute in an
            isolated namespace (possibly in concert with the ``imports``
            parameter)
        imports : list of strings
            list of import statements that allow the function to execute
            in an otherwise empty namespace
        """

        super(Function, self).__init__(**inputs)
        if function:
            if as_module:
                module = inspect.getmodule(function).__name__
                full_name = "%s.%s" % (module, function.__name__)
                self.inputs.function_str = full_name
            elif hasattr(function, '__call__'):
                try:
                    self.inputs.function_str = getsource(function)
                except IOError:
                    raise Exception('Interface Function does not accept '
                                    'function objects defined interactively '
                                    'in a python session')
                else:
                    if input_names is None:
                        fninfo = function.__code__
            elif isinstance(function, (str, bytes)):
                self.inputs.function_str = function
                if input_names is None:
                    fninfo = create_function_from_source(
                        function, imports).__code__
            else:
                raise Exception('Unknown type of function')

            if input_names is None:
                input_names = fninfo.co_varnames[:fninfo.co_argcount]

        self.as_module = as_module

        self.inputs.on_trait_change(self._set_function_string,
                                    'function_str')
        self._input_names = filename_to_list(input_names)
        self._output_names = filename_to_list(output_names)

        add_traits(self.inputs, [name for name in self._input_names])
        self.imports = imports
        self._out = {}
        
        for name in self._output_names:
            self._out[name] = None

    def _set_function_string(self, obj, name, old, new):
        if name == 'function_str':
            if self.as_module:
                module = inspect.getmodule(new).__name__
                full_name = "%s.%s" % (module, new.__name__)
                self.inputs.function_str = full_name
            elif hasattr(new, '__call__'):
                function_source = getsource(new)
                fninfo = new.__code__
            elif isinstance(new, (str, bytes)):
                function_source = new
                fninfo = create_function_from_source(
                    new, self.imports).__code__
            self.inputs.trait_set(trait_change_notify=False,
                                  **{'%s' % name: function_source})
            # Update input traits
            input_names = fninfo.co_varnames[:fninfo.co_argcount]
            new_names = set(input_names) - set(self._input_names)
            add_traits(self.inputs, list(new_names))
            self._input_names.extend(new_names)

    def _add_output_traits(self, base):
        undefined_traits = {}
        for key in self._output_names:
            base.add_trait(key, traits.Any)
            undefined_traits[key] = Undefined
        base.trait_set(trait_change_notify=False, **undefined_traits)
        return base

    def _run_interface(self, runtime):
        # Get workflow logger for runtime profile error reporting
        from nipype import logging
        logger = logging.getLogger('workflow')

        # Create function handle
        if self.as_module:
            import importlib
            pieces = self.inputs.function_str.split('.')
            module = '.'.join(pieces[:-1])
            function = pieces[-1]
            try:
                function_handle = getattr(importlib.import_module(module), function)
            except ImportError as e:
                raise RuntimeError('Could not import module: %s' % self.inputs.function_str)
        else:
            function_handle = create_function_from_source(self.inputs.function_str,
                                                          self.imports)

        # Wrapper for running function handle in multiprocessing.Process
        # Can catch exceptions and report output via multiprocessing.Queue
        def _function_handle_wrapper(queue, **kwargs):
            try:
                out = function_handle(**kwargs)
                queue.put(out)
            except Exception as exc:
                queue.put(exc)

        # Get function args
        args = {}
        for name in self._input_names:
            value = getattr(self.inputs, name)
            if isdefined(value):
                args[name] = value

        # Profile resources if set
        if runtime_profile:
            from nipype.interfaces.base import get_max_resources_used
            import multiprocessing
            # Init communication queue and proc objs
            queue = multiprocessing.Queue()
            proc = multiprocessing.Process(target=_function_handle_wrapper,
                                           args=(queue,), kwargs=args)

            # Init memory and threads before profiling
            mem_mb = 0
            num_threads = 0

            # Start process and profile while it's alive
            proc.start()
            while proc.is_alive():
                mem_mb, num_threads = \
                    get_max_resources_used(proc.pid, mem_mb, num_threads,
                                           pyfunc=True)

            # Get result from process queue
            out = queue.get()
            # If it is an exception, raise it
            if isinstance(out, Exception):
                raise out

            # Function ran successfully, populate runtime stats
            setattr(runtime, 'runtime_memory_gb', mem_mb / 1024.0)
            setattr(runtime, 'runtime_threads', num_threads)
        else:
            out = function_handle(**args)

        if len(self._output_names) == 1:
            self._out[self._output_names[0]] = out
        else:
            if isinstance(out, tuple) and (len(out) != len(self._output_names)):
                raise RuntimeError('Mismatch in number of expected outputs')

            else:
                for idx, name in enumerate(self._output_names):
                    self._out[name] = out[idx]

        return runtime

    def _list_outputs(self):
        outputs = self._outputs().get()
        for key in self._output_names:
            outputs[key] = self._out[key]
        return outputs
