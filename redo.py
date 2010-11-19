#!/usr/bin/python
import sys, os, subprocess, glob, time, random
import options, jwack, atoi

optspec = """
redo [targets...]
--
j,jobs=    maximum number of jobs to build at once
d,debug    print dependency checks as they happen
v,verbose  print commands as they are run
shuffle    randomize the build order to find dependency bugs
"""
o = options.Options('redo', optspec)
(opt, flags, extra) = o.parse(sys.argv[1:])

targets = extra or ['all']

if opt.debug:
    os.environ['REDO_DEBUG'] = str(opt.debug or 0)
if opt.verbose:
    os.environ['REDO_VERBOSE'] = '1'
if opt.shuffle:
    os.environ['REDO_SHUFFLE'] = '1'

is_root = False
if not os.environ.get('REDO_BASE', ''):
    is_root = True
    base = os.path.commonprefix([os.path.abspath(os.path.dirname(t))
                                 for t in targets] + [os.getcwd()])
    bsplit = base.split('/')
    for i in range(len(bsplit)-1, 0, -1):
        newbase = '/'.join(bsplit[:i])
        if os.path.exists(newbase + '/.redo'):
            base = newbase
            break
    os.environ['REDO_BASE'] = base
    os.environ['REDO_STARTDIR'] = os.getcwd()
    os.environ['REDO'] = os.path.abspath(sys.argv[0])


import vars, state
from helpers import *


if is_root:
    # FIXME: just wiping out all the locks is kind of cheating.  But we
    # only do this from the toplevel redo process, so unless the user
    # deliberately starts more than one redo on the same repository, it's
    # sort of ok.
    mkdirp('%s/.redo' % base)
    for f in glob.glob('%s/.redo/lock^*' % base):
        os.unlink(f)


class BuildError(Exception):
    pass
class BuildLocked(Exception):
    pass


def _possible_do_files(t):
    yield "%s.do" % t, t, ''
    dirname,filename = os.path.split(t)
    l = filename.split('.')
    l[0] = os.path.join(dirname, l[0])
    for i in range(1,len(l)+1):
        basename = '.'.join(l[:i])
        ext = '.'.join(l[i:])
        if ext: ext = '.' + ext
        yield (os.path.join(dirname, "default%s.do" % ext),
               os.path.join(dirname, basename), ext)


def find_do_file(t):
    for dofile,basename,ext in _possible_do_files(t):
        debug2('%s: %s ?\n' % (t, dofile))
        if os.path.exists(dofile):
            state.add_dep(t, 'm', dofile)
            return dofile,basename,ext
        else:
            state.add_dep(t, 'c', dofile)
    return None,None,None


def _preexec(t):
    os.environ['REDO_TARGET'] = os.path.basename(t)
    os.environ['REDO_DEPTH'] = vars.DEPTH + '  '
    dn = os.path.dirname(t)
    if dn:
        os.chdir(dn)


def _build(t):
    if (os.path.exists(t) and not state.is_generated(t)
          and not os.path.exists('%s.do' % t)):
        # an existing source file that is not marked as a generated file.
        # This step is mentioned by djb in his notes.  It turns out to be
        # important to prevent infinite recursion.  For example, a rule
        # called default.c.do could be used to try to produce hello.c,
        # which is undesirable since hello.c existed already.
        state.stamp(t)
        return  # success
    state.unstamp(t)
    state.start(t)
    (dofile, basename, ext) = find_do_file(t)
    if not dofile:
        raise BuildError('no rule to make %r' % t)
    state.stamp(dofile)
    unlink(t)
    tmpname = '%s.redo.tmp' % t
    unlink(tmpname)
    f = open(tmpname, 'w+')

    # this will run in the dofile's directory, so use only basenames here
    argv = ['sh', '-e',
            os.path.basename(dofile),
            os.path.basename(basename),  # target name (extension removed)
            ext,  # extension (if any), including leading dot
            os.path.basename(tmpname)  # randomized output file name
            ]
    if vars.VERBOSE:
        argv[1] += 'v'
        log_('\n')
    log('%s\n' % relpath(t, vars.STARTDIR))
    rv = subprocess.call(argv, preexec_fn=lambda: _preexec(t),
                         stdout=f.fileno())
    if rv==0:
        if os.path.exists(tmpname) and os.stat(tmpname).st_size:
            # there's a race condition here, but if the tmpfile disappears
            # at *this* point you deserve to get an error, because you're
            # doing something totally scary.
            os.rename(tmpname, t)
        else:
            unlink(tmpname)
        state.stamp(t)
    else:
        unlink(tmpname)
        state.unstamp(t)
    f.close()
    if rv != 0:
        raise BuildError('%s: exit code %d' % (t,rv))
    if vars.VERBOSE:
        log('%s (done)\n\n' % relpath(t, vars.STARTDIR))


def build(t):
    lock = state.Lock(t)
    lock.lock()
    if not lock.owned:
        log('%s (locked...)\n' % relpath(t, vars.STARTDIR))
        os._exit(199)
    try:
        try:
            return _build(t)
        except BuildError, e:
            err('%s\n' % e)
    finally:
        lock.unlock()
    os._exit(1)


def main():
    retcode = 0
    locked = {}
    waits = {}
    if vars.SHUFFLE:
        random.shuffle(targets)
    for t in targets:
        if os.path.exists('%s/all.do' % t):
            # t is a directory, but it has a default target
            t = '%s/all' % t
        waits[t] = jwack.start_job(t, lambda: build(t))
    jwack.wait_all()
    for t,pd in waits.items():
        assert(pd.rv != None)
        if pd.rv == 199:
            # target was locked
            locked[t] = 1
        elif pd.rv:
            err('%s: exit code was %r\n' % (t, pd.rv))
            retcode = 1
    for t in locked.keys():
        lock = state.Lock(t)
        lock.wait()
        relp = relpath(t, vars.STARTDIR)
        log('%s (...unlocked!)\n' % relp)
        if state.stamped(t) == None:
            err('%s: failed in another thread\n' % relp)
            retcode = 2
    return retcode


if not vars.DEPTH:
    # toplevel call to redo
    exenames = [os.path.abspath(sys.argv[0]), os.path.realpath(sys.argv[0])]
    if exenames[0] == exenames[1]:
        exenames = [exenames[0]]
    dirnames = [os.path.dirname(p) for p in exenames]
    os.environ['PATH'] = ':'.join(dirnames) + ':' + os.environ['PATH']

try:
    j = atoi.atoi(opt.jobs or 1)
    if j < 1 or j > 1000:
        err('invalid --jobs value: %r\n' % opt.jobs)
    jwack.setup(j)
    try:
        retcode = main()
    finally:
        jwack.force_return_tokens()
    if retcode:
        err('exiting: %d\n' % retcode)
    sys.exit(retcode)
except KeyboardInterrupt:
    sys.exit(200)
