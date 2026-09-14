import ast
import builtins
import os
import sys

PACKAGE = os.path.dirname(os.path.abspath(__file__))


def _module_scope(tree):
    names = set(dir(builtins))
    for node in tree.body:
        names.update(_bound_by(node))
    return names


def _bound_by(node):
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            names.add((alias.asname or alias.name).split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            names.add(alias.asname or alias.name)
    elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                names.update(_bound_by(child))
        if isinstance(node, ast.For):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
        if isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.name:
                    names.add(handler.name)
                for child in handler.body:
                    names.update(_bound_by(child))
    return names


def _local_scope(func):
    names = set()
    args = func.args
    for arg in args.args + args.kwonlyargs + args.posonlyargs:
        names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    for node in ast.walk(func):
        if isinstance(node, (ast.Name)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not func:
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _lambda_scope(func):
    args = func.args
    names = {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _check(func, visible, findings):
    own = _lambda_scope(func) if isinstance(func, ast.Lambda) else _local_scope(func)
    visible = visible | own
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in visible:
            findings.append((node.lineno, node.id))


def undefined_names(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    module = _module_scope(tree)
    findings = []
    stack = [(node, module) for node in ast.iter_child_nodes(tree)]
    while stack:
        node, visible = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            _check(node, visible, findings)
            inner = visible | (_lambda_scope(node) if isinstance(node, ast.Lambda) else _local_scope(node))
            for child in ast.iter_child_nodes(node):
                stack.append((child, inner))
        elif isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                stack.append((child, visible))
        else:
            for child in ast.iter_child_nodes(node):
                stack.append((child, visible))
    return sorted(set(findings))


def main(argv=None):
    root = argv[1] if argv and len(argv) > 1 else PACKAGE
    problems = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            for lineno, ident in undefined_names(path):
                print("%s:%d: name '%s' is not defined in this module" % (os.path.relpath(path), lineno, ident))
                problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
