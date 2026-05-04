import sys
import os
import subprocess
import json
import time

from importlib.metadata import packages_distributions, distribution, PackageNotFoundError
from packaging.requirements import Requirement
from .types import Metadata, ResourceType

PROFILING_SCRIPT = """
import sys
import time
import json
import os

dependencies, target = json.loads(sys.argv[1]), sys.argv[2]
for d in dependencies:
    try: __import__(d)
    except: pass

start = time.perf_counter()
try:
    __import__(target)
    duration = time.perf_counter() - start
    print(json.dumps({"time": duration}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""


def get_top_level_import_names(pkg_name):
    try:
        dist = distribution(pkg_name)
        top_level = dist.read_text('top_level.txt')
        if top_level:
            return [line.strip() for line in top_level.split('\\n') if line.strip()]
    except:
        pass
    return [pkg_name.replace('-', '_')]


def analyze_package(import_name, top_level_import, pkg_name) -> Metadata:
    try:
        dist = distribution(pkg_name)
    except PackageNotFoundError:
        return {"error": "Package not found"}

    # Dependencies
    requires = dist.requires or []
    dependencies = {}
    for req_str in requires:
        try:
            req = Requirement(req_str)
            if req.marker and not req.marker.evaluate():
                continue
            dependencies[req.name] = str(req.specifier) or "(any)"
        except:
            pass

    # Size of installed package
    size_bytes = 0
    import_path = import_name.replace('.', '/')
    if dist.files:
        for f in dist.files:
            if import_path == import_name or str(f).startswith(import_path):
                try:
                    path = dist.locate_file(f)
                    if os.path.isfile(path):
                        size_bytes += os.path.getsize(path)
                except:
                    pass

    # Import time
    dependency_imports = []
    for d in list(dependencies.keys()):
        dependency_imports.extend(get_top_level_import_names(d))

    try:
        out = subprocess.check_output([sys.executable, "-c", PROFILING_SCRIPT, json.dumps(
            dependency_imports), import_name], text=True).strip()
        metrics = json.loads(out)
    except:
        metrics = {}

    return {
        "base_import": top_level_import,
        "distribution": pkg_name,
        "dependencies": dependencies,
        "disk_size_mb": round(size_bytes / (1024 * 1024), 4),
        "load_time": metrics.get("time", 0),
        type: ResourceType.PACKAGE
    }


def analyze(packages: list[str]) -> dict[str, Metadata]:
    pkg_distributions = packages_distributions()
    all_distributions = set(list(pkg_distributions.keys()) + packages)

    results = {}
    for import_name in all_distributions:
        top_level_import = import_name.split('.')[0]
        if top_level_import not in pkg_distributions:
            pass
        results[import_name] = analyze_package(
            import_name, top_level_import, pkg_distributions.get(top_level_import)[0])
    return results
