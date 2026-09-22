import ast
from pathlib import Path


def test_holdout_runner_propagates_pipeline_exit_code():
    path = Path(__file__).resolve().parents[1] / "tools" / "run_holdout.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    returns = [
        node.value for node in ast.walk(main)
        if isinstance(node, ast.Return)
    ]
    assert any(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "int"
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "code"
        for value in returns
    )
