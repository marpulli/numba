"""
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
"""
from __future__ import print_function, absolute_import

import sys
import os
import warnings
from threading import RLock

import numpy as np

import llvmlite.llvmpy.core as lc
import llvmlite.binding as ll

from numba.npyufunc import ufuncbuilder
from numba.numpy_support import as_dtype
from numba import types, utils, cgutils, config

def get_thread_count():
    """
    Gets the available thread count.
    """
    t = config.NUMBA_NUM_THREADS
    if t < 1:
        raise ValueError("Number of threads specified must be > 0.")
    return t

NUM_THREADS = get_thread_count()


class ParallelUFuncBuilder(ufuncbuilder.UFuncBuilder):
    def build(self, cres, sig):
        _launch_threads()
        _init()

        # Buider wrapper for ufunc entry point
        ctx = cres.target_context
        signature = cres.signature
        library = cres.library
        fname = cres.fndesc.llvm_func_name

        ptr = build_ufunc_wrapper(library, ctx, fname, signature, cres)
        # Get dtypes
        dtypenums = [np.dtype(a.name).num for a in signature.args]
        dtypenums.append(np.dtype(signature.return_type.name).num)
        keepalive = ()
        return dtypenums, ptr, keepalive


def build_ufunc_wrapper(library, ctx, fname, signature, cres):
    innerfunc = ufuncbuilder.build_ufunc_wrapper(library, ctx, fname,
                                                 signature, objmode=False,
                                                 cres=cres)
    return build_ufunc_kernel(library, ctx, innerfunc, signature)


def build_ufunc_kernel(library, ctx, innerfunc, sig):
    """Wrap the original CPU ufunc with a parallel dispatcher.

    Args
    ----
    ctx
        numba's codegen context

    innerfunc
        llvm function of the original CPU ufunc

    sig
        type signature of the ufunc

    Details
    -------

    Generate a function of the following signature:

    void ufunc_kernel(char **args, npy_intp *dimensions, npy_intp* steps,
                      void* data)

    Divide the work equally across all threads and let the last thread take all
    the left over.


    """
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)

    intp_t = ctx.get_value_type(types.intp)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])
    wrapperlib = ctx.codegen().create_library('parallelufuncwrapper')
    mod = wrapperlib.create_ir_module('parallel.ufunc.wrapper')
    lfunc = mod.add_function(fnty, name=".kernel." + str(innerfunc))

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Release the GIL (and ensure we have the GIL)
    # Note: numpy ufunc may not always release the GIL; thus,
    #       we need to ensure we have the GIL.
    pyapi = ctx.get_python_api(builder)
    gil_state = pyapi.gil_ensure()
    thread_state = pyapi.save_thread()

    as_void_ptr = lambda arg: builder.bitcast(arg, byte_ptr_t)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    parallel_for_ty = lc.Type.function(lc.Type.void(),
                                            [byte_ptr_t] * 5 + [intp_t,] * 3)
    parallel_for = mod.get_or_insert_function(parallel_for_ty,
                                                    name='numba_parallel_for')

    # Note: the runtime address is taken and used as a constant in the function.
    fnptr = ctx.get_constant(types.uintp, innerfunc).inttoptr(byte_ptr_t)
    innerargs = [as_void_ptr(x) for x
                in [args, dimensions, steps, data]]
    builder.call(parallel_for, [fnptr] + innerargs +
                    [intp_t(x) for x in (len(sig.args), array_count,
                                        NUM_THREADS)])

    # Work is done. Reacquire the GIL
    pyapi.restore_thread(thread_state)
    pyapi.gil_release(gil_state)

    builder.ret_void()

    # Link and compile
    wrapperlib.add_ir_module(mod)
    wrapperlib.add_linking_library(library)
    return wrapperlib.get_pointer_to_function(lfunc.name)


# ---------------------------------------------------------------------------

class ParallelGUFuncBuilder(ufuncbuilder.GUFuncBuilder):
    def __init__(self, py_func, signature, identity=None, cache=False,
                 targetoptions={}):
        # Force nopython mode
        targetoptions.update(dict(nopython=True))
        super(ParallelGUFuncBuilder, self).__init__(py_func=py_func,
                                                    signature=signature,
                                                    identity=identity,
                                                    cache=cache,
                                                    targetoptions=targetoptions)

    def build(self, cres):
        """
        Returns (dtype numbers, function ptr, EnvironmentObject)
        """
        _launch_threads()
        _init()

        # Build wrapper for ufunc entry point
        ptr, env, wrapper_name = build_gufunc_wrapper(self.py_func, cres, self.sin, self.sout,
                                        cache=self.cache)

        # Get dtypes
        dtypenums = []
        for a in cres.signature.args:
            if isinstance(a, types.Array):
                ty = a.dtype
            else:
                ty = a
            dtypenums.append(as_dtype(ty).num)

        return dtypenums, ptr, env


def build_gufunc_wrapper(py_func, cres, sin, sout, cache):
    library = cres.library
    ctx = cres.target_context
    signature = cres.signature
    innerfunc, env, wrapper_name = ufuncbuilder.build_gufunc_wrapper(py_func, cres, sin, sout,
                                                       cache=cache)
    sym_in = set(sym for term in sin for sym in term)
    sym_out = set(sym for term in sout for sym in term)
    inner_ndim = len(sym_in | sym_out)

    ptr, name = build_gufunc_kernel(library, ctx, innerfunc, signature, inner_ndim)

    return ptr, env, name


def build_gufunc_kernel(library, ctx, innerfunc, sig, inner_ndim):
    """Wrap the original CPU gufunc with a parallel dispatcher.

    Args
    ----
    ctx
        numba's codegen context

    innerfunc
        llvm function of the original CPU gufunc

    sig
        type signature of the gufunc

    inner_ndim
        inner dimension of the gufunc

    Details
    -------

    Generate a function of the following signature:

    void ufunc_kernel(char **args, npy_intp *dimensions, npy_intp* steps,
                      void* data)

    Divide the work equally across all threads and let the last thread take all
    the left over.


    """
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)

    intp_t = ctx.get_value_type(types.intp)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])
    wrapperlib = ctx.codegen().create_library('parallelufuncwrapper')
    mod = wrapperlib.create_ir_module('parallel.gufunc.wrapper')
    lfunc = mod.add_function(fnty, name=".kernel." + str(innerfunc))

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Release the GIL (and ensure we have the GIL)
    # Note: numpy ufunc may not always release the GIL; thus,
    #       we need to ensure we have the GIL.
    pyapi = ctx.get_python_api(builder)
    gil_state = pyapi.gil_ensure()
    thread_state = pyapi.save_thread()

    as_void_ptr = lambda arg: builder.bitcast(arg, byte_ptr_t)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    parallel_for_ty = lc.Type.function(lc.Type.void(),
                                            [byte_ptr_t] * 5 + [intp_t,] * 3)
    parallel_for = mod.get_or_insert_function(parallel_for_ty,
                                                    name='numba_parallel_for')

    # Note: the runtime address is taken and used as a constant in the function.
    fnptr = ctx.get_constant(types.uintp, innerfunc).inttoptr(byte_ptr_t)
    innerargs = [as_void_ptr(x) for x
                in [args, dimensions, steps, data]]
    builder.call(parallel_for, [fnptr] + innerargs +
                    [intp_t(x) for x in (inner_ndim, array_count, NUM_THREADS)])

    # Release the GIL
    pyapi.restore_thread(thread_state)
    pyapi.gil_release(gil_state)

    builder.ret_void()

    wrapperlib.add_ir_module(mod)
    wrapperlib.add_linking_library(library)
    return wrapperlib.get_pointer_to_function(lfunc.name), lfunc.name


# ---------------------------------------------------------------------------

_backend_init_lock = RLock()

def _load_backend():
    if config.ENABLE_TBB != 0:
        try:
            from . import tbb_workqueue as lib
        except ImportError:
            msg = ("Intel TBB parallel back end was request but the module ",
                   "could not be imported. Falling back to the Numba parallel "
                   "back end")
            warnings.warn(msg)
            from . import workqueue as lib
    else:
        from . import workqueue as lib
    return lib

def _launch_threads():
    """
    Initialize work queues and workers
    """
    from ctypes import CFUNCTYPE, c_int

    with _backend_init_lock:
        lib = _load_backend()

        launch_threads = CFUNCTYPE(None, c_int)(lib.launch_threads)
        launch_threads(NUM_THREADS)


_is_initialized = False

def _init():
    with _backend_init_lock:
        lib = _load_backend()

        from ctypes import CFUNCTYPE, c_void_p

        global _is_initialized
        if _is_initialized:
            return

        ll.add_symbol('numba_parallel_for', lib.parallel_for)
        ll.add_symbol('do_scheduling_signed', lib.do_scheduling_signed)
        ll.add_symbol('do_scheduling_unsigned', lib.do_scheduling_unsigned)

        _is_initialized = True


_DYLD_WORKAROUND_SET = 'NUMBA_DYLD_WORKAROUND' in os.environ
_DYLD_WORKAROUND_VAL = int(os.environ.get('NUMBA_DYLD_WORKAROUND', 0))

if _DYLD_WORKAROUND_SET and _DYLD_WORKAROUND_VAL:
    _launch_threads()
