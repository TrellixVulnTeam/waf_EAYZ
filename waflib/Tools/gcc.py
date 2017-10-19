#!/usr/bin/env python
# encoding: utf-8
# Thomas Nagy, 2006-2010 (ita)
# Ralf Habacker, 2006 (rh)
# Yinon Ehrlich, 2009

"""
gcc/llvm detection.
"""

import os, sys
from waflib import Configure, Options, Utils
from waflib.Tools import ccroot, ar
from waflib.Configure import conf
from waflib.extras import gccdeps, visionflags

@conf
def find_gcc(conf):
	"""
	Find the program gcc, and if present, try to detect its version number
	"""
	cc = conf.find_program(['gcc', 'cc'], var='CC')
	conf.get_cc_version(cc, gcc=True)
	conf.env.CC_NAME = 'gcc'

@conf
def gcc_common_flags(conf):
	"""
	Common flags for gcc on nearly all platforms
	"""
	v = conf.env

	v['CC_SRC_F']            = []
	v['CC_TGT_F']            = ['-c', '-o']

	# linker
	if not v['LINK_CC']: v['LINK_CC'] = v['CC']
	v['CCLNK_SRC_F']         = []
	v['CCLNK_TGT_F']         = ['-o']
	v['CPPPATH_ST']          = '-I%s'
	v['DEFINES_ST']          = '-D%s'

	v['LIB_ST']              = '-l%s' # template for adding libs
	v['LIBPATH_ST']          = '-L%s' # template for adding libpaths
	v['STLIB_ST']            = '-l%s'
	v['STLIBPATH_ST']        = '-L%s'
	v['RPATH_ST']            = '-Wl,-rpath,%s'

	v['SONAME_ST']           = '-Wl,-h,%s'
	v['SHLIB_MARKER']        = '-Wl,-Bdynamic'
	v['STLIB_MARKER']        = '-Wl,-Bstatic'

	# program
	v['cprogram_PATTERN']    = '%s'

	# shared librar
	v['CFLAGS_cshlib']       = ['-fPIC']
	v['LINKFLAGS_cshlib']    = ['-shared']
	v['cshlib_PATTERN']      = 'lib%s.so'

	# static lib
	v['LINKFLAGS_cstlib']    = ['-Wl,-Bstatic']
	v['cstlib_PATTERN']      = 'lib%s.a'

	# osx stuff
	v['LINKFLAGS_MACBUNDLE'] = ['-bundle', '-undefined', 'dynamic_lookup']
	v['CFLAGS_MACBUNDLE']    = ['-fPIC']
	v['macbundle_PATTERN']   = '%s.bundle'

@conf
def gcc_modifier_win32(conf):
	"""Configuration flags for executing gcc on Windows"""
	v = conf.env
	v['cprogram_PATTERN']    = '%s.exe'

	v['cshlib_PATTERN']      = '%s.dll'
	v['implib_PATTERN']      = 'lib%s.dll.a'
	v['IMPLIB_ST']           = '-Wl,--out-implib,%s'

	v['CFLAGS_cshlib']       = []

	# Auto-import is enabled by default even without this option,
	# but enabling it explicitly has the nice effect of suppressing the rather boring, debug-level messages
	# that the linker emits otherwise.
	v.append_value('LINKFLAGS', ['-Wl,--enable-auto-import'])

@conf
def gcc_modifier_cygwin(conf):
	"""Configuration flags for executing gcc on Cygwin"""
	gcc_modifier_win32(conf)
	v = conf.env
	v['cshlib_PATTERN'] = 'cyg%s.dll'
	v.append_value('LINKFLAGS_cshlib', ['-Wl,--enable-auto-image-base'])
	v['CFLAGS_cshlib'] = []

@conf
def gcc_modifier_darwin(conf):
	"""Configuration flags for executing gcc on MacOS"""
	v = conf.env
	v['CFLAGS_cshlib']       = ['-fPIC']
	v['LINKFLAGS_cshlib']    = ['-dynamiclib', '-Wl,-compatibility_version,1', '-Wl,-current_version,1']
	v['cshlib_PATTERN']      = 'lib%s.dylib'
	v['FRAMEWORKPATH_ST']    = '-F%s'
	v['FRAMEWORK_ST']        = ['-framework']
	v['ARCH_ST']             = ['-arch']

	v['LINKFLAGS_cstlib']    = []

	v['SHLIB_MARKER']        = []
	v['STLIB_MARKER']        = []
	v['SONAME_ST']           = []

@conf
def gcc_modifier_aix(conf):
	"""Configuration flags for executing gcc on AIX"""
	v = conf.env
	v['LINKFLAGS_cprogram']  = ['-Wl,-brtl']
	v['LINKFLAGS_cshlib']    = ['-shared','-Wl,-brtl,-bexpfull']
	v['SHLIB_MARKER']        = []

@conf
def gcc_modifier_hpux(conf):
	v = conf.env
	v['SHLIB_MARKER']        = []
	v['STLIB_MARKER']        = '-Bstatic'
	v['CFLAGS_cshlib']       = ['-fPIC','-DPIC']
	v['cshlib_PATTERN']      = 'lib%s.sl'

@conf
def gcc_modifier_openbsd(conf):
	conf.env.SONAME_ST = []

@conf
def gcc_modifier_platform(conf):
	"""Execute platform-specific functions based on *gcc_modifier_+NAME*"""
	# * set configurations specific for a platform.
	# * the destination platform is detected automatically by looking at the macros the compiler predefines,
	#   and if it's not recognised, it fallbacks to sys.platform.
	gcc_modifier_func = getattr(conf, 'gcc_modifier_' + conf.env.DEST_OS, None)
	if gcc_modifier_func:
		gcc_modifier_func()

def options(opt):
	opt.load("visionflags")

def configure(conf):
	"""
	Configuration for gcc
	"""
	conf.find_gcc()
	conf.find_ar()
	conf.gcc_common_flags()
	conf.gcc_modifier_platform()
	# ECM: If CFLAGS/CCDEPS (or CXX) exist here, it has been provided by the
	# user. If we would load later, the env vars would have been already
	# touched by waf. We could have the idea to push the load into the c_config
	# file, but seems a bit too "intrusive"...
	conf.load("visionflags")
	conf.cc_load_tools()
	conf.cc_add_flags()
	conf.link_add_flags()
