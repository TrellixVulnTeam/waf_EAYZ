import os
from waflib import Build, Configure, Utils, Options, Logs
from waflib.extras import mr

MODULE_DIR = "modules"

#CACHE_DIR = os.path.join(self.build_path, Build.CACHE_DIR)

repo_tool = None
projects = set()
project_pathes = []

def options(ctx):
    global repo_tool
    repo_tool = mr.MR(ctx, True)
    repo_tool.register_top()
    projects.add( ('..', None) )

    # Patch options class
    ctx.__class__.load_project = load_project
    patchContextClass(ctx.__class__, "parse_args")

def load_project(ctx, name, branch = None):
    path = repo_tool.checkout_project(name, branch)
    projects.add( (name, branch) )
    project_pathes.append(path)
    ctx.recurse(path)

def configure(ctx):
    ctx.recurse(reversed(project_pathes))

@Configure.conf
def get_repo_tool(ctx):
    """Makes repo tool instance accessible for Configuartion and BuildContext
    This allows to use it in the MRContext
    """
    assert repo_tool
    return repo_tool

def patchContextClass(cls, fun):
    old = getattr(cls, fun)
    def new(ctx):
        old(ctx)
        all_projects = ((name, p.branch) for name, p in repo_tool.get_projects().items())
        old_projects = set(all_projects) - projects
        del all_projects
        repo_tool.remove_projects(old_projects)
    setattr(cls, fun, new)

