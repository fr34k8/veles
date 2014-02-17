"""
Created on Mar 12, 2013

Units in data stream neural network_common model.

@author: Kazantsev Alexey <a.kazantsev@samsung.com>
"""
from copy import copy
import os
from ply import lex
import pyopencl
import threading
import time
import traceback

import config
import cpp
import error
import logger
import opencl_types
import thread_pool


class Pickleable(logger.Logger):
    """Will save attributes ending with _ as None when pickling and will call
    constructor upon unpickling.
    """
    def __init__(self):
        """Calls init_unpickled() to initialize the attributes which are not
        pickled.
        """
        super(Pickleable, self).__init__()
        # self.init_unpickled()  # already called in Logger()

    """This function is called if the object has just been unpickled.
    """
    def init_unpickled(self):
        if hasattr(super(Pickleable, self), "init_unpickled"):
            super(Pickleable, self).init_unpickled()

    def __getstate__(self):
        """Selects the attributes to pickle.
        """
        state = {}
        for k, v in self.__dict__.items():
            if k[len(k) - 1] != "_" and not callable(v):
                state[k] = v
            else:
                state[k] = None
        return state

    def __setstate__(self, state):
        """Recovers the object after unpickling.
        """
        self.__dict__.update(state)
        self.init_unpickled()


class Distributable(object):
    """Callbacks for working in distributed environment.
    """
    def generate_data_for_master(self):
        """Data for master should be generated here. This function is executed
        on a slave instance.

        Returns:
            data of any type or None if there is nothing to send.
        """
        return None

    def generate_data_for_slave(self, slave=None):
        """Data for slave should be generated here. This function is executed
        on a master instance.

        Parameters:
            slave: some information about the slave (may be None).

        Returns:
            data of any type or None if there is nothing to send.
        """
        return None

    def apply_data_from_master(self, data):
        """Data from master should be applied here. This function is executed
        on a slave instance.

        Parameters:
            data - exactly the same value that was returned by
                   generate_data_for_slave at the master's side.

        Returns:
            None.
        """
        pass

    def apply_data_from_slave(self, data, slave=None):
        """Data from slave should be applied here. This function is executed
        on a master instance.

        Parameters:
            slave: some information about the slave (may be None).

        Returns:
            None.
        """
        pass


class Unit(Pickleable, Distributable):
    """General unit in data stream model.

    Attributes:
        links_from: dictionary of units it depends on.
        links_to: dictionary of dependent units.
        is_initialized: is_initialized unit or not.
        gate_lock_: lock.
        run_lock_: lock.
        gate_block: if [0] is true, open_gate() and run() will not be executed
                    and notification will not be sent further
                    ([0] can be a function).
        gate_skip: if [0] is true, open_gate() and run() will not be executed,
                   but notification will be sent further
                   ([0] can be a function).
        gate_block_not: if [0] is true, inverses conditions for gate_block
                   ([0] can be a function).
        gate_skip_not: if [0] is true, inverses conditions for gate_skip
                   ([0] can be a function).
    """

    pool_ = None
    pool_lock_ = threading.Lock()
    timers = {}

    @staticmethod
    def measure_time(fn, storage, key):
        def wrapped():
            sp = time.time()
            fn()
            fp = time.time()
            storage[key] += fp - sp

        return wrapped

    def __init__(self, workflow, **kwargs):
        if workflow != None and not isinstance(workflow, Unit):
            raise error.VelesException(
                "workflow parameter is not a Unit object")
        name = kwargs.get("name")
        view_group = kwargs.get("view_group")
        super(Unit, self).__init__()
        self.links_from = {}
        self.links_to = {}
        self.gate_block = [0]
        self.gate_skip = [0]
        self.gate_block_not = [0]
        self.gate_skip_not = [0]
        self.individual_name = name
        self.view_group = view_group
        self.workflow = workflow
        if self.workflow != None and hasattr(self.workflow, "add_ref"):
            self.workflow.add_ref(self)
        self.applied_data_from_master_recursively = False
        self.applied_data_from_slave_recursively = False
        setattr(self, "run", Unit.measure_time(getattr(self, "run"),
                                               Unit.timers, self))

    def __fini__(self):
        if self.workflow != None and hasattr(self.workflow, "del_ref"):
            self.workflow.del_ref(self)

    def init_unpickled(self):
        super(Unit, self).init_unpickled()
        self.gate_lock_ = threading.Lock()
        self.run_lock_ = threading.Lock()
        self.is_initialized = False
        Unit.timers[self] = 0

    def thread_pool(self):
        Unit.pool_lock_.acquire()
        try:
            if Unit.pool_ == None:
                Unit.pool_ = thread_pool.ThreadPool()
        finally:
            Unit.pool_lock_.release()
        return Unit.pool_

    def link_from(self, src):
        """Adds notification link.
        """
        self.links_from[src] = False
        src.links_to[self] = False

    @staticmethod
    def callvle(var):
        return var() if callable(var) else var

    def check_gate_and_run(self, src):
        """Check gate state and run if it is open.
        """
        if not self.open_gate(src):  # gate has a priority over skip
            return
        # Optionally skip the execution
        if ((Unit.callvle(self.gate_skip[0]) and
             (not Unit.callvle(self.gate_skip_not[0]))) or
            ((not Unit.callvle(self.gate_skip[0])) and
             Unit.callvle(self.gate_skip_not[0]))):
            self.run_dependent()
            return
        # If previous run has not yet finished, discard notification.
        if not self.run_lock_.acquire(blocking=False):
            return
        try:
            if not self.is_initialized:
                self.initialize()
                self.warning("%s is not initialized, performed the "
                                   "initialization", self.name())
                self.is_initialized = True
            self.run()
        finally:
            self.run_lock_.release()
        self.run_dependent()

    def initialize_dependent(self):
        """Invokes initialize() on dependent units on the same thread.
        """
        for dst in self.links_to.keys():
            if dst.is_initialized:
                continue
            if not dst.open_gate(self):
                continue
            dst.initialize()
            dst.is_initialized = True
            dst.initialize_dependent()

    def run_dependent(self):
        """Invokes run() on dependent units on different threads.
        """
        for dst in self.links_to.keys():
            if ((Unit.callvle(dst.gate_block[0]) and
                 (not Unit.callvle(dst.gate_block_not[0]))) or
                ((not Unit.callvle(dst.gate_block[0])) and
                 Unit.callvle(dst.gate_block_not[0]))):
                continue
            self.thread_pool().callInThread(dst.check_gate_and_run, self)

    def initialize(self):
        """Allocate buffers here.

        initialize() invoked in the same order as run(), including
        open_gate() and effects of gate_block and gate_skip.

        If initialize() succeeds, self.is_initialized flag will be
        automatically set.
        """
        pass

    def run(self):
        """Do the job here.
        """
        pass

    def open_gate(self, src):
        """Called before run() or initialize().

        Returns:
            True: gate is open, can call run() or initialize().
            False: gate is closed, run() and initialize() shouldn't be called.
        """
        self.gate_lock_.acquire()
        if not len(self.links_from):
            self.gate_lock_.release()
            return True
        if src in self.links_from:
            self.links_from[src] = True
        if not all(self.links_from.values()):
            self.gate_lock_.release()
            return False
        for src in self.links_from:  # reset activation flags
            self.links_from[src] = False
        self.gate_lock_.release()
        return True

    def unlink_from_all(self):
        """Unlinks self from other units.
        """
        self.gate_lock_.acquire()
        for src in self.links_from:
            del(src.links_to[self])
        for dst in self.links_to:
            del(dst.links_from[self])
        self.links_from.clear()
        self.links_to.clear()
        self.gate_lock_.release()

    def unlink_from(self, src):
        """Unlinks self from src.
        """
        self.gate_lock_.acquire()
        if self in src.links_to:
            del src.links_to[self]
        if src in self.links_from:
            del self.links_from[src]
        self.gate_lock_.release()

    def nothing(self, *args, **kwargs):
        """Function that do nothing.

        It may be used to overload some methods to do nothing.
        """
        pass

    def log_error(self, **kwargs):
        """Logs any errors.
        """
        if "msg" in kwargs.keys():
            self.error(kwargs["msg"])
        if "exc_info" in kwargs.keys():
            exc_info = kwargs["exc_info"]
            traceback.print_exception(exc_info[0], exc_info[1], exc_info[2])

    def name(self):
        if self.individual_name:
            return self.individual_name
        return self.__class__.__name__

    def set_name(self, value):
        if not isinstance(value, str):
            raise ValueError("Unit name must be a string")
        self.individual_name = value


class OpenCLUnit(Unit):
    """Unit that operates using OpenCL.

    Attributes:
        device: Device object.
        prg_: OpenCL program.
        cl_sources: OpenCL source files: file => defines.
        prg_src: last built OpenCL program source code text.
        run_executed: sets to True at the end of run.
    """
    def __init__(self, workflow, **kwargs):
        device = kwargs.get("device")
        kwargs["device"] = device
        super(OpenCLUnit, self).__init__(workflow, **kwargs)
        self.device = device
        self.prg_src = None
        self.run_executed = False

    def init_unpickled(self):
        super(OpenCLUnit, self).init_unpickled()
        self.prg_ = None
        self.cl_sources_ = {}

    def cpu_run(self):
        """Run on CPU only.
        """
        return super(OpenCLUnit, self).run()

    def gpu_run(self):
        """Run on GPU.
        """
        return self.cpu_run()

    def run(self):
        t1 = time.time()
        if self.device:
            self.gpu_run()
        else:
            self.cpu_run()
        self.run_executed = True
        self.debug("%s in %.2f sec" % (self.__class__.__name__,
                                             time.time() - t1))

    @staticmethod
    def read_ocl_file(file_name):
        fin = None
        for path in config.ocl_dirs:
            try:
                fin = open(os.path.join(path, file_name), "r")
            except:
                continue
            break
        if not fin:
            raise error.VelesException(
                "\"%s\" was not found in any of the following paths: %s" % (
                                    file_name, ", ".join(config.ocl_dirs)[2:]))
        s = fin.read()
        fin.close()
        return s

    def build_program(self, defines=None, dump_preprocessed=None):
        """Builds OpenCL program.

        _prg will be is_initialized with built program.

        Parameters:
            defines: additional definitions.
            log_fnme: file to write constructed source to.
            s_append: string to append to constructed source.

        Returns:
            Built OpenCL program source code.
        """
        # write the skeleton
        source = ""
        if defines and not isinstance(defines, dict):
            raise RuntimeError("defines must be a dictionary")
        my_defines = copy(defines) if defines else {}
        for file, defs in self.cl_sources_.items():
            source += '#include "%s"\n' % file
            my_defines.update(defs)
        my_defines.update(opencl_types.cl_defines[config.c_dtype])

        # initialize C preprocessor
        lexer = lex.lex(cpp)
        cprep = cpp.Preprocessor(lexer)
        for name, value in my_defines.items():
            cprep.define("%s %s" % (name, value))
        cprep.path = copy(config.ocl_dirs)
        cprep.parse(source, "opencl")

        # record includes
        files_list = [('opencl', 0)]
        while True:
            token = cprep.token()
            if not token:
                break
            if files_list[-1][0] != cprep.source:
                lineno = 1
                if token.lineno >= 3:
                    lineno = token.lineno - 1
                files_list.append((cprep.source, lineno))

        # bake the inclusion plan
        flatten_plan = []
        file_bottoms = {}
        for file in reversed(files_list[1:-1]):
            if file[0] == 'opencl':
                continue
            bottom = file_bottoms.get(file[0])
            if not bottom:
                flatten_plan.insert(0, (file[0], -1))
            else:
                lines = bottom - file[1]
                flatten_plan.insert(0, (file[0], lines))
            file_bottoms[file[0]] = file[1] - 1

        # execute the plan
        preprocessed_source = ''.join('#define {} {}\n'.format(key, val)
                                      for key, val in my_defines.items())
        opened_files = {}
        for incl in flatten_plan:
            file = opened_files.get(incl[0])
            if not file:
                file = open(incl[0])
                opened_files[incl[0]] = file
            if incl[1] == -1:
                preprocessed_source += file.read()
                file.seek(0)
            else:
                for _ in range(0, incl[1]):
                    preprocessed_source += file.readline()
                file.readline()  # skip the #include line
        for key, val in opened_files.items():
            val.close()

        # debug the merged sources
        if dump_preprocessed != None:
            flog = open(dump_preprocessed, "w")
            flog.write(preprocessed_source)
            flog.close()

        # compile OpenCL program from the merged sources
        self.prg_src = preprocessed_source
        self.prg_ = pyopencl.Program(self.device.context_,
                                     self.prg_src).build()

    def get_kernel(self, name):
        return pyopencl.Kernel(self.prg_, name)


class Repeater(Unit):
    """Repeater.
    TODO(v.markovtsev): add more detailed description
    """

    def __init__(self, workflow, **kwargs):
        kwargs["view_group"] = kwargs.get("view_group", "PLUMBING")
        super(Repeater, self).__init__(workflow, **kwargs)

    def open_gate(self, src):
        """Gate is always open.
        """
        return True
