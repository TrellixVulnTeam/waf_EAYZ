#!/usr/bin/env python
# encoding: utf-8

"""
Dependencies system

A :py:class:`waflib.Dependencies.DependenciesContext` instance is created when ``waf dependencies`` is called, it is used to:

"""

import os, sys
from waflib import Utils, Logs, Context, Options, Configure, Errors
import json
import tempfile
import re
import shutil
from distutils.version import LooseVersion

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

import subprocess

try:
    from ConfigParser import RawConfigParser
except ImportError:
    from configparser import RawConfigParser
from waflib.extras.symwaf2ic_misc import parse_gerrit_changes

# will be set from symwaf2ic
get_repo_tool = lambda: None


class Repo_DB(object):
    def __init__(self, filepath):
        self.db = json.load(open(filepath, "r"))

    def get_init(self, name):
        return self.db[name].get("init_cmds",'')

    def get_clone_depth(self, name):
        clone_depth = self.db[name].get("clone_depth", None)
        if clone_depth is not None:
            assert clone_depth.isdigit()
            clone_depth = int(clone_depth)
            if (clone_depth == 0 or clone_depth < -1):
                raise ValueError("Clone depth from repo db {} not in valid range [-1, 1, 2, ...]".format(self.clone_depth))
        return clone_depth

    def get_aliases(self, name):
        return self.db[name].get("aliases", [])

    def get_type(self, name):
        return self.db[name]["type"]

    def get_url(self, name):
        return self.db[name]["url"]

    def get_description(self, name):
        return self.db[name].get("description") or "- n/a -"

    def get_manager(self, name):
        return self.db[name].get("manager")

    def get_default_branch(self, name):
        return self.db[name].get("default_branch", None)

    def list_repos(self):
        names = self.db.keys()
        return filter(lambda x: not x.startswith("_"), names)


class BranchError(Exception):
    pass


class Project(object):
    def __init__(self, name, path, branch = None, ref = None, clone_depth = -1):
        assert isinstance(name, str)
        assert os.path.isabs(path)
        self._name = name
        self._path = path
        self.ref = ref
        self._branch = branch
        self._real_branch = None
        self._mr_registered = False
        self._clone_depth = clone_depth
        self._gerrit_changes = []
        self.required = False

    def __str__(self):
        try:
            return self.name + " {" + self.required_branch + "}"
        except BranchError:
            return self.name + " {???}"

    def __eq__(self, another):
        return self.name == another.name

    def __hash__(self):
        return hash(self.name)

    @property
    def name(self):
        return self._name

    @property
    def mr_registered(self):
        return self._mr_registered

    @mr_registered.setter
    def mr_registered(self, value):
        self._mr_registered = value

    @property
    def required_branch(self):
        if self._branch is None:
            raise BranchError("required branch unknown")
        return self._branch

    @required_branch.setter
    def required_branch(self, branch):
        if self._branch is None:
            self._branch = branch if branch is not None else self.default_branch
        elif branch is None:
            pass
        elif self._branch != branch:
            raise BranchError("branch already set")
        else:
            pass

    @property
    def required_gerrit_changes(self):
        return self._gerrit_changes

    @required_gerrit_changes.setter
    def required_gerrit_changes(self, changes):
        # the option parser verifies the format
        self._gerrit_changes = tuple(changes)

    @property
    def path(self):
        """Absolute path of the repository"""
        return self._path

    @property
    def real_branch(self):
        if self._real_branch is None:
            ret, stdout, stderr = self.exec_cmd(self.get_branch_cmd())
            if ret != 0:
                err = "{} returned {}\n{}{}".format(' '.join(self.get_branch_cmd()),
                        ret, stdout, stderr)
                raise RuntimeError(err)
            self._real_branch =  stdout.strip()
        return self._real_branch

    @property
    def clone_depth(self):
        if self.ref:
            Logs.warn("\nProject {project} is to be checked out at reference "
                      "{ref}. Shallow clone omitted.".format(project=self.name,
                                                             ref=self.ref))
            return -1

        return self._clone_depth

    def update_branch(self, force=False):
        cmd = self.reset_branch_cmd() if force else self.set_branch_cmd()
        ret, stdout, stderr = self.exec_cmd(cmd)
        if ret != 0:
            raise BranchError(cmd + ":\n" + stdout + stderr)
        self._real_branch = None

    def update_gerrit_changes(self, gerrit_url):
        for cmd in self.gerrit_changes_cmds(gerrit_url):
            ret, stdout, stderr = self.exec_cmd(cmd, shell=True)
            if ret != 0:
                raise BranchError(cmd + ":\n" + stdout + stderr)

    def path_from(self, start):
        assert os.path.isabs(start)
        return os.path.relpath(self.path, start)

    def exec_cmd(self, cmd, **kw):
        defaults = {
                'cwd'    : self.path,
                'stdout' : subprocess.PIPE,
                'stderr' : subprocess.PIPE,
            }
        defaults.update(kw)
        Logs.debug('mr: Running "{cmd}" with env {env}'.format(
            cmd=cmd, env=defaults))
        p = subprocess.Popen(cmd, **defaults)
        stdout, stderr = p.communicate()
        return p.returncode, \
               stdout.decode(sys.stdout.encoding or "utf-8"), \
               stderr.decode(sys.stderr.encoding or "utf-8")

    # TO IMPLEMENT
    def mr_checkout_cmd(self, *k, **kw):
        raise AttributeError

    def mr_init_cmd(self, *k, **kw):
        raise AttributeError

    def mr_update_cmd(self, *k, **kw):
        """Implement custom update command if needed.

        The function should return a string containing the custom update
        command or None in which case mr will fallback to the default.
        """
        return None


class GitProject(Project):
    vcs = 'git'
    default_branch = 'master'

    def get_branch_cmd(self):
        return ['git', 'rev-parse', '--abbrev-ref', 'HEAD']

    def set_branch_cmd(self, branch = None):
        return ['git', 'checkout', branch if branch else self.required_branch]

    def reset_branch_cmd(self, branch = None):
        return ['git', 'checkout', '--force', branch if branch else self.required_branch]

    def gerrit_changes_cmds(self, gerrit_url):
        fetch_cmd = 'git fetch {BASE_URL}/{PROJECT} {REF}'
        apply_cmd = 'git {} FETCH_HEAD'
        checkout_cmd = 'checkout'
        cherry_pick_cmd = 'cherry-pick --allow-empty --keep-redundant-commits'

        # Previously, if there is more than one changeset for a project, the
        # first changeset is checked out and all others are cherry-picked. This
        # leads to errors in the following case:
        #
        # ProjectX: review/master -> A -> B `----`--- Depends-On: C ProjectY:
        # review/master -> C `--- Depends-On: A
        #
        # In the current setup, when we want to check out B, waf detects B's
        # dependency on C and checks it out. Then it discovers C's dependency
        # on A and tries to checkout A in ProjectX which - with the current
        # strategy - amounts to cherry-picking A on B which will fail in almost
        # any case.
        #
        # The solution is to discover that the already checked out B is
        # actually a descendant of A - so by keeping B checked out we also meet
        # the requirement of A being checked out.
        #
        # The full strategy is:
        # * check if one changeset is an ancestor of current HEAD or vice versa
        #   -> switch to the "younger" commit
        # * otherwise:
        #   * checkout on first changeset
        #   * cherry-pick the current changeset onto HEAD otherwise
        #
        # Unfortunately, this strategy needs to be implemented as one single
        # bash command that is executed by mr.

        check_ancestry_cmd = 'git merge-base --is-ancestor {ancestor} {descendant}'

        # checkout the descendant (and exit 0) if the two commits are related
        checkout_descendant_cmd = \
            "if {cmd}; then git checkout {{descendant}}; exit 0; fi".format(
                cmd=check_ancestry_cmd)

        def generate_apply_changeset_cmd(cmd_no_relation):
            # execute command in subshell so that we can use `exit 0`
            return "({})".format('; '.join([
                checkout_descendant_cmd.format(
                    ancestor='FETCH_HEAD', descendant='HEAD'),
                checkout_descendant_cmd.format(
                    ancestor='HEAD', descendant='FETCH_HEAD'),
                cmd_no_relation
                ]))

        first_commit = True

        for changeset in self.required_gerrit_changes:
            cref = changeset['currentPatchSet']['ref']
            project = changeset['project']
            yield fetch_cmd.format(BASE_URL=gerrit_url.geturl(), PROJECT=project, REF=cref)
            if first_commit:
                yield generate_apply_changeset_cmd(apply_cmd.format(checkout_cmd))
                first_commit = False
            else:
                yield generate_apply_changeset_cmd(apply_cmd.format(cherry_pick_cmd))

    def __init__(self, *args, **kw):
        super(self.__class__, self).__init__(*args, **kw)

    def mr_checkout_cmd(self, base_node, url, clone_depth):
        path = self.path_from(base_node)
        depth = clone_depth
        depth = '--depth {}'.format(depth) if depth >= 0 else ''
        cmd = ["git clone --branch '{branch}' {depth} '{url}' '{target}'".format(
            branch=self.required_branch, depth=depth,
            url=url, target=os.path.basename(path))]
        return 'checkout=%s' % "; ".join(cmd)

    def mr_init_cmd(self, init, gerrit_url=None):
        cmds = list()
        if init:
            cmds.append(init)
        if self.ref:
            cmds.append('git reset --hard {}'.format(self.ref))
        if gerrit_url:
            cmds += self.gerrit_changes_cmds(gerrit_url)
        if not cmds:
            # no post_checkout needed...
            return ''
        ret = ["post_checkout = cd {name}".format(
            name=os.path.basename(self.name))]
        ret += cmds
        return " && ".join(ret)

    def mr_update_cmd(self, remote=None, branch=None, *a, **kw):
        return "git pull --rebase {remote} {branch}".format(
            remote=remote if remote is not None else "origin",
            branch=branch if branch is not None else self.required_branch)


class MR(object):
    MR         = "mr"
    MR_LOCAL_DIR = '.myrepos'
    # MR_CONFIG  = "repo.conf"
    MR_CONFIG  = ".symwaf2ic.repo.conf"
    MR_LOG     = "repo.log"
    DB_FOLDER  = "repo_db"
    DB_FILE    = "repo_db.json"
    # MODULE_DIR = "modules"
    CFGFOLDER  = "mr_conf"
    SCRIPTS    = "scripts.d"
    LOG_COLOR  = "BLUE"
    LOG_WARN_COLOR  = "ORANGE"
    GIT_MIN_VERSION = "1.7.11"

    project_types = {
            'git' : GitProject
    }

    def __init__(self, ctx, db_url="git@example.com:db.git", db_type="git",
                 top=None, cfg=None, clear_log=False, clone_depth=None, gerrit_url=None):
        # Note: Don't store the ctx. It gets finalized before MR
        if not top:
            top = getattr(ctx, 'srcnode', None)
        if not top:
            top = ctx.path
        if not top:
            ctx.fatal("Could not find top dir")

        # INIT dirs: DO NOT store nodes, as each context brings its one node hierarchy
        self.base = top.abspath()
        self.config = top.make_node(self.MR_CONFIG).abspath()
        self.log = cfg.make_node(self.MR_LOG).abspath()
        script_dir = cfg.make_node(self.SCRIPTS)
        script_dir.mkdir()
        self.scripts = script_dir.abspath()

        self.check_git_version(ctx)

        self.find_mr(ctx)
        self.projects = {}
        if clear_log:
            with open(self.log, 'w') as log:
                log.write("")

        Logs.debug('mr: commands are logged to "%s"' % self.log)

        self.clone_depth = clone_depth

        if isinstance(gerrit_url, str):
            self.gerrit_url = urlparse.urlparse(gerrit_url)
        elif isinstance(gerrit_url, urlparse.ParseResult):
            self.gerrit_url = gerrit_url
        else:
            ctx.fatal("Unsupported type for gerrit_url: \"{}\"".format(type(gerrit_url)))

        self.setup_repo_db(ctx, cfg, top, db_url, db_type)

        self.init_mr()
        Logs.debug("mr: Found managed repositories: {}".format(self.pretty_projects()))

    def load_projects(self):
        parser = self.load_config()
        projects = self.projects
        for name in parser.sections():
            projects[name] = self._get_or_create_project(name)
            projects[name].mr_registered = True

    def find_mr(self, ctx):
        waflib_node = ctx.root.find_node(os.path.join(Context.waf_dir, 'waflib'))
        mr_tool = waflib_node.find_node(os.path.join('bin', 'mr'))
        if not mr_tool:
            ctx.fatal("Your symwaf2ic-waflib seems to be corrupted, could not find mr tool!")
        Logs.debug('mr: Using "%s" to manage repositories' % mr_tool.abspath())
        self.mr_path = mr_tool.parent.abspath()

    def check_git_version(self, ctx):
        """
        Verify that the git version required for this class is available

        note: We use LooseVersion for comparision, because some git versions
              have 4 numbers
        """
        cmd_git_version = "git --version"
        output = ctx.cmd_and_log(cmd_git_version.split(),
            output=Context.STDOUT, quiet=Context.STDOUT)

        match = re.search(r'\b[\d.]+\b', output)
        if not match:
            ctx.fatal("Could not parse git version in output of \"{}\":\n{}".format(
                cmd_git_version, output))

        version_string = match.group()
        if not LooseVersion(version_string) >= LooseVersion(self.GIT_MIN_VERSION):
            ctx.fatal("Minimum git version required is git {MIN} (> {CUR})".format(
                MIN=self.GIT_MIN_VERSION, CUR=version_string))

    def setup_repo_db(self, ctx, cfg, top, db_url, db_type):
        # first install some mock object that servers to create the repo db repository
        class MockDB(object):
            def get_clone_depth(self, *k, **kw):
                return None
            def get_url(self, *k, **kw):
                return db_url
            def get_init(self, *k, **kw):
                return ""
            def get_type(self, *k, **kw):
                return db_type
        self.db = MockDB()

        db_node = cfg.make_node(self.DB_FOLDER)
        db_path = db_node.path_from(top)
        if db_type == "wget":
            # TODO: implement download via wget # KHS: should I do this?
            raise Errors.WafError("wget support not implemented yet. Poke obreitwi!")
        else:
            # see if db repository is already checked out, if not, add it
            # since we have not read all managed repositories, manually read the mr config
            parser = self.load_config()
            self.projects[db_path] = db_repo = self.project_types[db_type](
                name=db_path, path=db_node.abspath())
            db_repo.required_branch = None
            db_repo.required = True
            if db_path not in parser.sections() or not os.path.isdir(db_repo.path):
                # we need to add it manually because if project isn't found we would look in the
                # not yet existing db
                self.mr_checkout_project(ctx, db_repo)

        self.db = Repo_DB(os.path.join(db_node.abspath(), self.DB_FILE))

    def init_mr(self):
        self.init_default_config()
        self.load_projects()
        not_on_filesystem = []
        for name, p in self.projects.items():
            if not os.path.isdir(p.path):
                not_on_filesystem.append(name)
        if not_on_filesystem:
            Logs.debug("mr: Projects not on file system: {}".format(not_on_filesystem))
            self.remove_projects(not_on_filesystem)

    def init_default_config(self):
        parser = self.load_config()
        parser.set('DEFAULT', 'git_log', 'git log -n1 "$@"')
        self.save_config(parser)

    def mr_log(self, msg, sep = "\n"):
        for m in msg.split('\n'): Logs.debug('mr: ' + m)
        with open(self.log, 'a') as log:
            log.write(msg)
            log.write(sep)

    def mr_print(self, msg, color = None, sep = '\n'):
        self.mr_log(msg, '\n')
        Logs.pprint(color if color else self.LOG_COLOR, msg, sep = sep)

    def load_config(self):
        """Load mr config file, returns an empty config if the file does not exits"""
        parser = RawConfigParser()
        parser.read([self.config])
        return parser

    def save_config(self, parser):
        with open(self.config, 'w') as cfg_file:
            parser.write(cfg_file)

    def format_cmd(self, *args, **kw):
        """ use _conf_file to override config file destination """
        conf_file = kw.pop("_conf_file", os.path.relpath(self.config, self.base))

        # like this (env mr instead of absolute path) we can assert that the environment has been
        # passed correctly (i.e. PATH with our-mr inserted - mr sometimes calles itself).
        cmd = ['env', 'mr', '-t', '-c', conf_file]
        cmd.extend(args)

        self.mr_log('-' * 80 + '\n' + "{}".format(cmd) + ':\n')

        kw['cwd']    = self.base
        kw['env']    = dict(self.get_mr_env())
        return cmd, kw

    def getMrScript(self, *args):
        """returns abspath to a bash script, each arg representing one line of code"""

        fn = Utils.to_hex(Utils.h_list(args)) + "0.sh" # hash-of-args + version of getMrScript
        fullpath = os.path.join(self.scripts, fn)

        if not os.path.exists(fullpath):
            with open(fullpath, 'w') as out:
                out.write('''#!/bin/bash
# This file was generated by mr.py - getMrScript
''')
                for arg in args:
                    out.write(arg)
                    out.write('\n')
            os.chmod(fullpath, 0o754)
            Logs.debug("mr: script created " + fullpath)
        else:
            Logs.debug("mr: script reused " + fullpath)

        return fullpath

    def call_mr(self, ctx, *args, **kw):
        self.mr_log("dispatching mr command: {} -- {}".format(args, kw))

        tmpfile = None
        if args and args[0] == "register":
            # because mr seems to have a bug not trusting any config file
            # during "register" we write the config to a tempfile and append manually .. ¬_¬

            # NOTE: we can be sure that register is only called if the project is not present
            # in the config file

            if sys.version_info < (3, 0):
                tmpfile = tempfile.NamedTemporaryFile()
            else:
                tmpfile = tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8')
            kw["_conf_file"] = tmpfile.name

        cmd, kw = self.format_cmd(*args, **kw)
        kw['quiet']  = Context.BOTH
        kw['output'] = Context.BOTH
        try:
            Logs.debug("mr: executing in: " + kw['cwd'] + " -- with first PATH segment set to: " + kw['env']['PATH'].split(':')[0])
            stdout, stderr = ctx.cmd_and_log(cmd, **kw)
        except Errors.WafError as e:
            stdout = getattr(e, 'stdout', "")
            stderr = getattr(e, 'stderr', "")
            self.mr_log('command:\n"%s"\nstdout:\n"%s"\nstderr:\n"%s"\n' % (' '.join(cmd), stdout, stderr))
            Logs.error('command:\n"%s"\nstdout:\n"%s"\nstderr:\n"%s"\n' % (' '.join(cmd), stdout, stderr))
            if stderr:
                e.msg += ':\n\n' + stderr
            if tmpfile is not None:
                tmpfile.close()
            raise e

        msg = 'stdout:\n"' + stdout + '"\n'
        msg += 'stderr:\n"' + stderr + '"\n'
        self.mr_log(msg)

        if tmpfile is not None:
            # write config to repo conf
            tmpfile.seek(0)
            tmpfile_lines = tmpfile.file.readlines()
            tmpfile.close()
            #for i,v in enumerate(tmpfile_lines):
            #    Logs.debug("mr: tmpfile {}: {}".format(i,v))

            # make sure path in header is relative (as if we had registered it without
            # all the 'security' shennanigans from mr)
            header_idx = 1
            path = tmpfile_lines[header_idx].strip()[1:-1]
            Logs.debug("mr: originally registered path: {}".format(path))
            node = ctx.root.find_node(path)

            # KHS: Fixing weird behaviour of mr register. If executed in a subdir of /tmp or outside
            # of $HOME -- not sure what exactly the cause is, it registers repos as
            # [toplevel/repodir] instead of [/root/path/to/repodir].
            if not node:
                assert path.startswith(ctx.path.encode('utf-8'))
                node=ctx.path.parent.find_node(path)
                Logs.debug('mr: wierd mr-register-behaviour-fix applied.')
                assert node # or fix failed

            tmpfile_lines[header_idx] = "[{0}]\n".format(
                os.path.relpath(node.abspath(), self.base))
            Logs.debug("mr: registered {}".format(tmpfile_lines[header_idx]))
            with open(self.config, 'a') as cfg_file:
                for line in tmpfile_lines:
                    cfg_file.write(line)

        return cmd, stdout, stderr

    def get_mr_env(self):
        env = os.environ
        path = env["PATH"].split(os.pathsep)
        path.insert(0, self.mr_path)
        env["PATH"] = os.pathsep.join(path)
        return env # KHS: jihaa function was a noop (return statement was missing)

    def resolve_gerrit_changes(self, ctx, gerrit_queries, ignored_cs=None):
        """
        Perform queries on gerrit to find the all changesets.
        Returns a dictionary containing the changesets sorted indexed by
        project names and containing the ordered set of changesets (same order
        as the queries).
        :param ignored_cs: List of changeset numbers to be ignored
        :type ignored_cs: set of int or list of int
        """
        assert self.gerrit_url.scheme == 'ssh'
        ssh = "ssh {H}".format(H=self.gerrit_url.hostname)
        if self.gerrit_url.username:
            ssh += ' -l {U}'.format(U=self.gerrit_url.username)
        else:
            # If there's a [gitreview] username, use that one
            git_p = subprocess.Popen(["git", "config", "gitreview.username"],
                                     stdout=subprocess.PIPE)
            review_user, _ = git_p.communicate()
            review_user.decode(sys.stdout.encoding or "utf-8")
            if git_p.returncode == 0:
                ssh += ' -l {U}'.format(U=review_user.strip())
        if self.gerrit_url.port:
            ssh += ' -p {P}'.format(P=self.gerrit_url.port)
        query_options = '--current-patch-set --dependencies --format=json'

        seen = set() if ignored_cs is None else set(ignored_cs)
        # changesets are storted as per-project-list inside a dict
        # => per-project order is preserved!
        changesets = dict()
        for gerrit_query in gerrit_queries:
            cmd = "{S} gerrit query {Q} {OPT}".format(
                S=ssh, Q=gerrit_query, OPT=query_options)
            ret = ctx.cmd_and_log(cmd, shell=True, output=Context.STDOUT, quiet=Context.STDOUT)
            Logs.debug('mr: {C}'.format(C=cmd))
            Logs.debug('mr: {R}'.format(R=ret))
            data = [json.loads(line) for line in ret.split('\n') if line]

            # we get at least one answer row
            if len(data) == 0:
                ctx.fatal("Failure for query '{Q}': no response from server".format(
                    Q=gerrit_query))

            # the last line is the stats or error field
            stats = data.pop()
            if stats.get('type') == 'error' or 'rowCount' not in stats:
                ctx.fatal("Failure for query '{Q}'. Query failed: {E}".format(
                    Q=gerrit_query, E=stats))
            if stats['rowCount'] == 0:
                ctx.fatal("No results for query '{Q}'".format(Q=gerrit_query))

            # additional consistency check (cannot happen in normal cases)
            assert stats['rowCount'] == len(data)

            self.mr_print("Resolved query \"{Q}\":".format(Q=gerrit_query))
            for result in data:
                if result['number'] in seen:
                    continue
                seen.add(result['number'])

                changesets.setdefault(result['project'], []).append(result)
                rev = result['currentPatchSet']['number']
                msg = result['commitMessage'].splitlines()[0][:70]
                self.mr_print("\tChangeset {project}:{number}/{rev} \"{msg}\"".format(
                    rev=rev, msg=msg, **result))
                self.mr_print("\t    {url}".format(**result))

        # --- Cross-project dependencies of all changesets --- #
        cross_queries = list()
        for cs in sum(changesets.values(), list()):
            commit_msg = cs['commitMessage']

            # Get changeset query strings
            for line in commit_msg.splitlines():
                if line.startswith('Depends-On:'):
                    queries_raw = line[len('Depends-On:'):].strip()
                    cross_queries += parse_gerrit_changes(queries_raw)

        # Recursion termination: continue only if there are unseen changes left
        if cross_queries:
            # Resolve query strings, ignore those already seen
            cross_cs = self.resolve_gerrit_changes(ctx, cross_queries,
                                                   ignored_cs=seen)

            # Add all cross-repo changesets to the seen changes and the
            # results list
            for project, cross_cs in cross_cs.items():
                for changeset in cross_cs:
                    seen.add(changeset['number'])
                    changesets.setdefault(project, []).append(changeset)

        return changesets

    def checkout_project(self, ctx, project, parent_path, branch=None, ref=None,
                         update_branch=False, gerrit_changes=None):
        p = self._get_or_create_project(project)
        p.required = True
        try:
            p.required_branch = branch
        except BranchError:
            self.mr_print('Project "%s" is already required on branch "%s", but "%s" requires branch "%s"'\
                    % ( project, p.required_branch, parent_path, branch), 'YELLOW')

        p.ref = ref

        required_gerrit_changes = []
        if gerrit_changes:
            for project_name in [p.name] + self.db.get_aliases(p.name):
                tmp = gerrit_changes.get(project_name, [])
                required_gerrit_changes += tmp


        if p.mr_registered and os.path.isdir(p.path) and os.listdir(p.path):
            if update_branch and p.required_branch != p.real_branch:
                self.mr_print('Switching branch of repository %s from %s to %s..' % \
                        ( project, p.real_branch, p.required_branch), sep = '')
                try:
                    p.update_branch(force=bool(update_branch=='force'))
                except BranchError as e:
                    self.mr_print('')
                    ctx.fatal("In project {p}: {err}".format(p=p.name, err=e))
                self.mr_print('done', 'GREEN')

            if p.required_gerrit_changes != required_gerrit_changes:
                p.required_gerrit_changes = required_gerrit_changes
                try:
                    p.update_gerrit_changes(self.gerrit_url)
                except BranchError as e:
                    self.mr_print('')
                    ctx.fatal("In project {p}: {err}".format(p=p.name, err=e))

            return p.path_from(self.base)
        else:
            p.required_gerrit_changes = required_gerrit_changes
            return self.mr_checkout_project(ctx, p)

    def mr_checkout_project(self, ctx, p):
        "Perform the actual mr checkout"
        path = p.path_from(self.base)
        do_checkout = False
        if '-h' in sys.argv or '--help' in sys.argv:
            Logs.warn('Not all projects were found: the help message may be incomplete')
            ctx = Context.create_context('options')
            ctx.parse_args()
            sys.exit(0)

        # Check if the project folder exists, in this case the repo
        # needs only to be registered
        if os.path.isdir(p.path):
            self.mr_print("Registering pre-existing repository '%s'..." % p, sep = '')
            Logs.debug('mr: ') # better output if mr zone is active
            self.call_mr(ctx, 'register', path)
        else:
            do_checkout = True
            self.mr_print("Checking out repository %s {%s} to '%s'..."
                % (self.db.get_url(p.name), p.required_branch, p.name), sep = '')

        if self.clone_depth:
            clone_depth = self.clone_depth
        else:
            db_clone_depth = self.db.get_clone_depth(p.name)
            clone_depth = db_clone_depth if db_clone_depth else -1

        args = ['config', p.name,
                p.mr_checkout_cmd(self.base, self.db.get_url(p.name), clone_depth)
               ]
        init_cmd = p.mr_init_cmd(self.db.get_init(p.name), self.gerrit_url)
        if init_cmd:
            args += [init_cmd]
        self.call_mr(ctx, *args)

        update_cmd = p.mr_update_cmd()

        if update_cmd is not None:
            self.call_mr(ctx, 'config', p.name, "update={}".format(update_cmd))

        if do_checkout:
            try:
                self.call_mr(ctx, 'checkout')
            except Errors.WafError:
                self.mr_print('failed', 'RED')
                self.mr_print('Removing incomplete checkout: {0}'.format(p.path))
                shutil.rmtree(p.path, ignore_errors=False)
                raise

        p.mr_registered = True
        self.mr_print('done', 'GREEN')
        return path

    def remove_projects(self, projects):
        parser = self.load_config()
        for name in projects:
            if not name in self.projects:
                continue
            p = self.projects[name]
            self.mr_print("Remove repository %s from repo.conf" % p.name)
            parser.remove_section(p.path_from(self.base))
            del self.projects[name]

        self.save_config(parser)

    def clean_projects(self):
        names = [p.name for p in self.projects.values() if not p.required]
        self.remove_projects(names)

    def get_wrong_branches(self):
        ret = []
        for name, p in self.projects.items():
            try:
                if p.required_branch != p.real_branch:
                    ret.append( (name, p.real_branch, p.required_branch) )
            except BranchError:
                pass
        return ret

    def get_projects(self):
        return self.projects

    def pretty_projects(self):
        names = []
        for name, p in self.projects.items():
            names.append(self.pretty_name(p))
        return ", ".join(names).encode('utf-8')

    def pretty_name(self, prj):
        out = prj.name + " {on " + prj.real_branch + "}"
        return out

    # def _repo_node(self, name):
        # """returns a a node representing the repo folder"""
        # node = self.base.make_node(name)
        # return node

    def _get_or_create_project(self, name):
        ret = self.projects.get(name, None)
        if ret:
            return ret

        try:
            vcs = self.db.get_type(name)
        except KeyError as e:
            Logs.error("Missing information in repository database: %s" % name)
            raise KeyError("Missing information in repository database. Missing key: '%s'" % e.message)

        p = self.project_types[vcs](name=name, path=os.path.join(self.base, name), clone_depth=self.clone_depth)
        default_branch = self.db.get_default_branch(name)
        if default_branch is not None:
            p.default_branch = default_branch
        self.projects[name] = p
        return p


# TODO: KHS, this is not a build step, its a configure step if any specific step at all.
# Subclassing ConfigurationContext states the intention more clearly -
# and serves better my purpose

class MRContext(Configure.ConfigurationContext):
    '''MR adapter for symwaf2ic'''
    cmd = None
    cmd_prefix_args = None
    debug=False # set to True to print the command prior execution.

    # KHS: this is a noop
    #def __init__(self, **kw):
    #    super(MRContext, self).__init__(**kw)

    def execute(self):
        """
        See :py:func:`waflib.Context.Context.execute`.
        """
        self.mr = get_repo_tool()

        cmd, kw = self.mr.format_cmd(*self.get_args())
        if self.debug:
            Logs.info(cmd)
        subprocess.call(cmd, **kw)

    def get_args(self):
        ret = []
        if self.cmd_prefix_args:
            ret += Utils.to_list(self.cmd_prefix_args)
        ret += Utils.to_list(getattr(self, 'mr_cmd', self.cmd.replace('repos-','')))
        return ret


class mr_run(MRContext):
    '''runs rargs in all repositories (./waf mr-run -- your command)'''
    cmd = 'mr-run'
    mr_cmd = 'run'      # + Options.rargs

    def get_args(self):
        if not Options.rargs:
            self.fatal("Usage: %s. Maybe you forgot the '--' separator?" % (self.__doc__))
        ret = [ 'run' ] + Options.rargs
        Options.rargs=[]
        self.mr_cmd = ' '.join(ret)
        return ret


class mr_xrun(MRContext):
    '''create shell script from rargs and run this in every repository (./waf mr-xrun -- "line1" "line2" ...)'''
    cmd = 'mr-xrun'
    mr_cmd = 'run'      # run <path_to_mrcmd_node>
    mr_cmds = []        # ie. read from Options.rargs, override this in subclasses

    def __init__(self, **kw):
        super(mr_xrun, self).__init__(**kw)

        # Node for mrcmd scripts (shell scripts to be called by mr for complex commands)
        self.init_dirs() # sets bldnode
        self.mrcmd_node = self.bldnode.make_node('.mrcmd')
        self.mrcmd_node.mkdir()

    def getMrCmdFile(self):
        if not self.mr_cmds:
            Logs.debug('mr: get commands {}'.format(Options.rargs))
            self.mr_cmds = Options.rargs
            Options.rargs=[]

        if not self.mr_cmds:
            self.fatal("Usage: %s. Maybe you forgot the '--' separator?" % (self.__doc__))

        from waflib.extras import symwaf2ic
        script = symwaf2ic.storage.repo_tool.getMrScript(*self.mr_cmds)
        return script

    def get_args(self):
        return ['run', self.getMrCmdFile()]


class mr_origin_log(mr_xrun):
    """Get log messages from correspondant origin branch (does not fetch, ./waf repos-origin-log [-- <log-format-options>])"""

    cmd = "repos-origin-log"
    mr_cmds = [ "ref=`git symbolic-ref -q HEAD` # refs/heads/<branchname>",
                #"# upstream: The name of a local ref which can be considered “upstream” from the displayed ref (KHS: ie, origin)",
                "branch=`git for-each-ref --format='%(upstream:short)' $ref` # origin/<branchname>",
                "git log $@ $branch" # $@: commandline argument (logformat)
    ]

    def get_args(self):
        if Options.rargs:
            logformat = ' '.join(Options.rargs)
            Options.rargs=[]
        else:
            logformat = "-n1 --pretty=oneline"

        return ['run', self.getMrCmdFile(), logformat]


class mr_status(MRContext):
    '''check status of the repositories (using MR tool)'''
    cmd = 'repos-status'
    # reduce verbosity (no empty lines)
    cmd_prefix_args = '--minimal'


class mr_fetch(MRContext):
    '''updates origin in all repositories (git fetch --no-progress)'''
    cmd = 'repos-fetch'
    # KHS: --tags removed as this somehow disables fetch
    mr_cmd = 'run git fetch --no-progress'


class mr_up(MRContext):
    '''update the repositories (using MR tool)'''
    cmd = 'repos-update'


class mr_diff(MRContext):
    '''diff all repositories (using MR tool)'''
    cmd = 'repos-diff'
    cmd_prefix_args = '--minimal'


class mr_commit(MRContext):
    '''commit all changes (using MR tool)'''
    cmd = 'repos-commit'


class mr_push(MRContext):
    '''push all changes (using MR tool)'''
    cmd = 'repos-push'


class mr_log(MRContext):
    '''call log for all repositories (using MR tool)'''
    cmd = 'repos-log'


class mr_lstag(MRContext):
    '''lists all tags of all repos'''
    cmd = 'repos-lstag'
    mr_cmd = 'run git tag --list'


def options(opt):
    gr = opt.add_option_group("show_repos")
    gr.add_option(
        "--manager", dest="show_repos_manager", action="store_true",
        help="Also list the managers of the repositories.",
        default=False
    )
    gr.add_option(
        "--url", dest="show_repos_url", action="store_true",
        help="Also list the urls of the repositories.",
        default=False
    )
    gr.add_option(
        "--full-description", dest="show_repos_fdesc", action="store_true",
        help="List the full description of the repositories, no matter what.",
        default=False
    )


class show_repos_context(Context.Context):
    __doc__ = '''lists all available repositories'''
    cmd = 'show_repos'
    def __init__(self, **kw):
        super(show_repos_context, self).__init__(**kw)


    def build_repo_info(self, r):
        info = {"name" : r,
                "used" : '*' if (r in self.used) else ' ',
                "desc" : self.db.get_description(r),
                "url"  : self.db.get_url(r),
                "man"  : self.db.get_manager(r) or '- n/a -',
        }
        return info

    def get_longest_field(self, d, key):
        if d:
            item = max(d, key = lambda x: len(x[key]))
            return len(item[key])
        else:
            return 0

    def truncate_field(self, data, field, length):
        cut = max(length - 3, 0)
        for k in data:
            f = k[field]
            if len(f) > length:
                k[field] = f[:cut] + "..."

    def truncate_statistical(self, data, field, sd_factor=1.3, length = None):
        """truncate field on one sd from mean"""
        # KHS: naja... hab mich ein bischen verkünstelt...
        sm = 0
        for k in data: sm += len(k[field])
        mv = sm / float(len(data))
        sd = 0
        for k in data: sd+=(mv - len(k[field]))**2
        sd = (sd/len(data))**0.5
        l = int( mv + sd_factor * sd )
        if length:
            length=min(l, length)
        else:
            length=l
        return self.truncate_field(data, field, length)

    def execute(self):
        """
        See :py:func:`waflib.Context.Context.execute`.
        """
        self.mr = get_repo_tool()
        self.db = self.mr.db

        self.repos = sorted(self.db.list_repos())
        self.used = set(self.mr.get_projects().keys())

        try:
            columns = int(os.getenv("STTY_COLUMNS", 0))
            if not columns:
                columns = int(os.popen('stty size', 'r').read().split()[1]) # 0 are the rows.
        except:
            #test -t 0 && ... otherwise it fails (if stdin is not there - like in jenkins)
            if os.getenv("JOB_URL"):
                columns = 160 # jenkins is wide
            else:
                Logs.warn("Could not determine console width ('stty size' failed), defaulting to 80.")
                columns = 80 # very basic size uh...

        data = [ self.build_repo_info(r) for r in self.repos ]

        strip = Options.options.show_repos_url + Options.options.show_repos_manager # 0,1,2
        if (not Options.options.show_repos_fdesc) and strip:
            self.truncate_statistical(data, "desc", 2.7-strip, 57-(10*strip))

        field = "{{{name}: <{len}}}"
        fields = [ ("name", self.get_longest_field(data, "name")),
                 #  ("used", 6),
                   ("desc", self.get_longest_field(data, "desc")),
        #           ("url", self.get_longest_field(data, "url")),
        #           ("man", self.get_longest_field(data, "man")),
        ]

        if Options.options.show_repos_url:
            fields.append( ("url", self.get_longest_field(data, "url")) )
        if Options.options.show_repos_manager:
            fields.append( ("man", self.get_longest_field(data, "man")) )

        line = "| {used} " + " | ".join([field.format(name = n, len = l) for n, l in fields]) + " |"

        header = line.format(name = "Repository", used = " ", desc = "Description", url = "url", man = "Manager")

        if len(header)>columns:
            Logs.info("Your console width is not wide enough for a beautiful output or 'stty size' failed...")
            line = " {used} " + "\n   ".join([field.format(name = n, len = l) for n, l in fields]) + "\n"
            header = line.format(name = "Repository", used = " ", desc = "Description", url = "url", man = "Manager")
            header += "-" * columns
        else:
            header += '\n' + "-" * len(header)

        print(header)
        for d in data:
            print(line.format(**d))
