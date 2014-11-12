"""
    Externs encapsulate the mapping between Python function and an
    "external" function available in a library/shared-object.

    Externs can be "materialized" the process of materialization will
    allways result in the same thing: a mapping form a Python function
    to a function within a library/shared-object.

        * "Just" a mapping to an existing library
        * Compilation of C or Chapel source-code
        * Compilation of inline C or Chapel source-code

    Compilation is done dynamically either just-in-time, when the mapped
    function is called. Or ahead-of-time, when the mapped function is
    decorated. Or somewhere in between those to points in time.
"""
import logging
import hashlib
import inspect
import pprint
import ctypes
import os

from pych.exceptions import MaterializationError
from pych.types import PychArray, TYPEMAP, KEYWORDS, np
import pych.runtime

#
# Decorate a Python-function with these to construct a mapping to an Extern.
#
class Extern(object):
    """Encapsulates a mapping and call to an external function."""

    def __init__(self, ename=None, lib=None, sfile=None, slang=None):

        # This is done only once; when the function is decorated
        self.pfunc = None   # Python function handle
        self.pname = None   # Python function name
        self.doc = None     # Python doc-string (used for inline source-code)
        self.atypes = []    # Python types of function arguments
        self.anames = []    # Python names of function arguments
        self.rtype = None   # Python type for the return value

        self.efunc = None   # ctypes function handle
        self.ename = ename  # External function name
        self.lib = lib      # Library filename

        self.sfile = sfile  # File with sourcecode
        self.slang = slang  # Language of the sourcefile or inline
        self.source = None  # Sourcecode in textual representation

        # For externs mapped to inline or file-based sourcecode.
        self.dec_fn = None  # Filename including the full path to the file
        self.dec_ts = 0     # Modification timestamp
        self.dec_hs = None  # Hash of the filename

        if not pych.RT:
            msg = "pyChapel runtime is not available, "\
                  "extern-decorators won't work."
            raise Exception(msg)

    def __repr__(self):
        return pprint.pformat(vars(self))

    def _validate_decl(self):
        """Validate the function declaration."""

        if len(self.atypes) != len(self.anames):
            # Check that we have sufficient amount of type-declarations
            raise TypeError("Missing type declaration on arguments.")
        else:
            # Check that declared types are supported and that the
            # argument-names does not conflict with keywords.
            for i, arg in enumerate(self.anames):
                # Keyword
                if self.slang in KEYWORDS and arg in KEYWORDS[self.slang]:
                    raise TypeError("Extern arg('%s') shadows %s keyword." % (
                        arg,
                        self.slang.capitalize()
                    ))
                # Type
                arg_type = self.atypes[i]
                if arg_type not in TYPEMAP:
                    msg = "Unsupported type: %s for argument %s" % (
                        arg_type, arg
                    )
                    raise TypeError(msg)

        # Check the function-name
        if self.ename and (self.ename in KEYWORDS[self.slang] or
                           self.ename.endswith("_synced")):
            raise TypeError("Extern ename('%s') shadows %s keyword." % (
                self.ename, self.slang.capitalize()
            ))

    def _type_check(self, args):
        """Compare argument-types with extern declation."""

        call_types = [type(arg) for arg in args]
        
        if call_types != self.atypes:
            ct_text = pprint.pformat(call_types)
            at_text = pprint.pformat(self.atypes)
            raise TypeError("Unsupported arg-types; %s. Expected: %s" %
                            (ct_text, at_text))

    def __call__(self, pfunc):
        """
        Sets up the function-mapping and hints the runtime of its existance.
        Invoked upon decoration.
        """
        # This is done only once; when the function is decorated

        self.pfunc = pfunc
        self.pname = pfunc.__name__
        self.doc = pfunc.__doc__

        #
        # Infer type-declaration of Extern from the crazy convention
        arg_spec = inspect.getargspec(self.pfunc)

        self.anames = arg_spec.args         # Extract argument names
        if arg_spec.defaults:               # Extract argument types
            self.atypes = list(arg_spec.defaults)
        self.rtype = self.pfunc()           # Obtain return-type

        #
        # Extract attributes for "inline" function
        #
        # TODO: Consider library-naming, we want to persist across
        #       executions, and really want to do the impossible:
        #       expand the inline-library. So what can be done instead?
        #
        #       Collisions should be avoided yet so should compilation also.
        #

        #
        # Construct the library-name based on inline or sfile.
        #
        # Use dec_fn and dec_ts to determine is a sfile or inline has changed.
        if self.sfile:  # Determine file to use to detect changes
            self.dec_fn = self.sfile
        else:
            self.dec_fn = self.pfunc.func_globals["__file__"]
        
        if self.dec_fn: # Construct hash of filename to use as identifier
            dec_hash = hashlib.md5()
            dec_hash.update(self.dec_fn)

            self.dec_ts = int(os.stat(self.dec_fn).st_mtime)
            self.dec_hs = dec_hash.hexdigest()

        if self.sfile or self.doc:  # Construct the filename for the library
            # TODO: Consider parsing the source-file and expanding validation
            #       of the type-declaration using the parsed information.
            if not self.ename:
                self.ename = self.pname
            liborigin = "sfile" if self.sfile else "inline"
            self.lib = "%s-%s-%s-%s.so" % (
                liborigin,
                self.slang,
                self.dec_hs,
                self.dec_ts
            )

        self._validate_decl()               # Validate declaration

        #
        # Hint the runtime that we might want to call this Extern in
        # the future.
        # This could be used as a means of compiling the
        # function ahead of time. Or compile all hinted functions
        # in one go as the first call hits a function.
        #
        pych.RT.hint(self)

        def wrapped_f(*args):
            """The logic invoked when calling a mapped function."""

            self._type_check(args)      # Typecheck actuals with declaration

            #
            # Obtain the external object.
            if not self.efunc:
                efunc = pych.RT.materialize(self)

                if not efunc:
                    raise MaterializationError(self)
                #
                # Register argument conversion functions on efunc
                efunc.argtypes = [TYPEMAP[atype] for atype in self.atypes]
                efunc.restype = TYPEMAP[self.rtype]

                #
                # Register the efunc handle on Extern
                self.efunc = efunc

            # Convert the nptypes
            c_args = []
            for arg in args:
                if type(arg) is np.ndarray:
                    mapped_array = pych.RT.map_nparray(arg)
                    c_args.append(mapped_array)
                else:
                    c_args.append(arg)

            #
            # Call
            return self.efunc(*c_args)

        return wrapped_f

class FromC(Extern):
    """
    Decorate a Python function using this Extern to map it to an
    external C function.
    """

    def __init__(self, ename=None, lib=None, sfile=None):
        super(FromC, self).__init__(
            ename=ename,
            lib=lib,

            sfile=sfile,
            slang="c"
        )

class Chapel(Extern):
    """
    Decorate a Python function using this Extern to map it to an
    external Chapel function.
    """

    def __init__(self, ename=None, lib=None, sfile=None):
        super(Chapel, self).__init__(
            ename=ename,
            lib=lib,

            sfile=sfile,
            slang="chapel"
        )

