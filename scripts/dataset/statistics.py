import json

with open("./dataset.json", 'r') as f:
    data = json.load(f)

category_counts = {}
# Unique resources
packages, datasets, models, tokenizers = {}, {}, {}, {}

for item in data:
    category = item['category']
    category_counts[category] = category_counts.get(category, 0) + 1

    for pkg in item['imports']:
        pkg = pkg.split('.')[0]  # Get the top-level package
        if pkg not in packages:
            packages[pkg] = { "count": 0, "categories": set() }
        packages[pkg]["count"] = packages.get(pkg)["count"] + 1
        packages[pkg]["categories"].add(category)
    for ds in item['datasets']:
        if ds not in datasets:
            datasets[ds] = { "count": 0, "categories": set() }
        datasets[ds]["count"] = datasets.get(ds)["count"] + 1
        datasets[ds]["categories"].add(category)
    for model in item['models']:
        if model not in models:
            models[model] = { "count": 0, "categories": set() }
        models[model]["count"] = models.get(model)["count"] + 1
        models[model]["categories"].add(category)
    for tokenizer in item['tokenizers']:
        if tokenizer not in tokenizers:
            tokenizers[tokenizer] = { "count": 0, "categories": set() }
        tokenizers[tokenizer]["count"] = tokenizers.get(tokenizer)["count"] + 1
        tokenizers[tokenizer]["categories"].add(category)

print("Category Distribution:")
total = 0
for category, count in category_counts.items():
    print(f"{category}: {count}")
    total += count
print(f"Total: {total}")

print(f"\nUnique Resources:")
print(f"Packages: ({len(packages)}) {sorted(packages.items(), key=lambda x: x[1]['count'], reverse=True)[:15]}")
print(f"Datasets: ({len(datasets)}) {sorted(datasets.items(), key=lambda x: x[1]['count'], reverse=True)[:10]}")
print(f"Models: ({len(models)}) {sorted(models.items(), key=lambda x: x[1]['count'], reverse=True)[:10]}")
print(f"Tokenizers: ({len(tokenizers)}) {sorted(tokenizers.items(), key=lambda x: x[1]['count'], reverse=True)[:10]}")