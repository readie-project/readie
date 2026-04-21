import ast

class TreeParser(ast.NodeVisitor):
    def __init__(self):
        self.imports = set()
        self.datasets = set()
        self.tokenizers = set()
        self.models = set()
        self.transformers_variables = set()

    def _get_class_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        return None

    def _extract_string_arg(self, node):
        # Positional arg
        if node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value

        # Keyword arg
        for kw in node.keywords:
            if kw.arg in {"pretrained_model_name_or_path", "model_name"}:
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
        return None

    def visit_Import(self, node: ast.Import):
        for name in node.names:
            self.imports.add(name.name)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self.imports.add(node.module)
        if node.module == "transformers":
            for alias in node.names:
                self.transformers_variables.add(alias.asname or alias.name)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr

            if method == "from_pretrained":
                class_name = self._get_class_name(node.func.value)

                if class_name:
                    model_name = self._extract_string_arg(node)

                    if model_name:
                        if "Tokenizer" in class_name:
                            self.tokenizers.add(model_name)
                        elif "Model" in class_name:
                            self.models.add(model_name)
        self.generic_visit(node)

    def start(self, code):
        # Clean code
        lines = code.splitlines()
        cleaned_lines = []

        for line in lines:
            if line.startswith("%") or line.startswith("!") or line.startswith("?"):
                continue
            if line.startswith("%%"):
                continue
            if line.startswith("#"):
                if line.startswith("# DATASET USED:"):
                    self.datasets.add(line[len("# DATASET USED:"):].strip())
                continue
            cleaned_lines.append(line)

        code = "\n".join(cleaned_lines)

        node = ast.parse(code)
        self.generic_visit(node)
        
        return {
            "imports": list(self.imports),
            "datasets": list(self.datasets),
            "tokenizers": list(self.tokenizers),
            "models": list(self.models)
        }