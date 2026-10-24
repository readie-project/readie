// Sample programs for the playground. Each one imports libraries that Readie
// checkpoints already carry, so a restored run skips most of the import time.
// They use inline data (the sandbox has no network) and finish in a few seconds.
// Each one is from a category of the request corpus that the pipeline plans checkpoints from.

export type Example = {
  id: string;
  // One of the categories of the request corpus that the pipeline plans checkpoints from.
  domain: string;
  title: string;
  imports: string;
  code: string;
};

export const examples: Example[] = [
  {
    id: 'entities',
    domain: 'Natural Language Processing',
    title: 'Prices and emails',
    imports: 'spaCy',
    code: `import spacy
from spacy.matcher import Matcher

# A blank English pipeline: tokenizer plus rule-based matching, no model to download.
nlp = spacy.blank("en")
matcher = Matcher(nlp.vocab)
matcher.add("PRICE", [[{"TEXT": "$"}, {"LIKE_NUM": True}]])
matcher.add("EMAIL", [[{"LIKE_EMAIL": True}]])

text = (
    "Order #482 for $49.99 shipped to anna@example.com. "
    "A refund of $12 was sent to ben@example.org."
)
doc = nlp(text)
for match_id, start, end in matcher(doc):
    print(f"{nlp.vocab.strings[match_id]:<6} {doc[start:end].text}")
`,
  },
  {
    id: 'correlation',
    domain: 'Data Visualization',
    title: 'Height and weight',
    imports: 'seaborn, SciPy',
    code: `import io

import matplotlib
import numpy as np
import seaborn as sns
from scipy import stats

matplotlib.use("Agg")

rng = np.random.default_rng(1)
height = rng.normal(170, 8, 300)
weight = 0.9 * height - 90 + rng.normal(0, 6, 300)

r, _ = stats.pearsonr(height, weight)
ax = sns.regplot(x=height, y=weight, scatter_kws={"s": 8})
png = io.BytesIO()
ax.figure.savefig(png, format="png")
print(f"Correlation between height and weight: {r:.2f}")
print(f"Chart: {len(png.getvalue()) / 1024:.0f} KB")
`,
  },
  {
    id: 'scan',
    domain: 'Image Processing',
    title: 'Scan clean-up',
    imports: 'Pillow, pandas',
    code: `import numpy as np
import pandas as pd
from PIL import Image, ImageFilter, ImageOps

# A noisy, dim 400x300 scan, made in memory.
rng = np.random.default_rng(3)
scan = Image.fromarray(rng.integers(20, 90, (300, 400), dtype=np.uint8))

# Clean it up: blur the noise, then stretch the contrast.
clean = ImageOps.autocontrast(scan.filter(ImageFilter.GaussianBlur(2)))

report = pd.DataFrame(
    {"before": np.asarray(scan).std(), "after": np.asarray(clean).std()}, index=["contrast"]
)
print(report.round(1).to_string())
print("Size:", clean.size)
`,
  },
  {
    id: 'keywords',
    domain: 'Natural Language Processing',
    title: 'Review keywords',
    imports: 'spaCy',
    code: `from collections import Counter

import spacy

# A blank English pipeline: tokenizer and built-in stop words, no model to download.
nlp = spacy.blank("en")
reviews = [
    "The battery life is great, but the screen scratches easily.",
    "Great screen, great battery. The charger feels cheap though.",
    "Battery died after a week. The screen is fine.",
]

# Count the meaningful words across all reviews.
words = Counter(
    token.lower_
    for doc in nlp.pipe(reviews)
    for token in doc
    if token.is_alpha and not token.is_stop
)
for word, count in words.most_common(4):
    print(f"{word:<10} {count}")
`,
  },
  {
    id: 'customers',
    domain: 'Data Preprocessing',
    title: 'Clean customer records',
    imports: 'pandas, numpy, re',
    code: `import re

import numpy as np
import pandas as pd

customers = pd.DataFrame({
    "name": ["  ana  SMITH", "BEN lee", None, "cho Park"],
    "phone": ["(555) 123-4567", "555.987.6543", "n/a", "5551112222"],
    "age": [34, np.nan, 29, 41],
})

# Tidy the names, keep only the digits of each phone number, and fill missing ages.
customers["name"] = customers["name"].str.strip().str.title().fillna("Unknown")
digits = customers["phone"].map(lambda p: re.sub(r"\\D", "", p))
customers["phone"] = digits.where(digits.str.len() == 10)
customers["age"] = customers["age"].fillna(customers["age"].median())
print(customers.to_string(index=False))
`,
  },
  {
    id: 'tickets',
    domain: 'Natural Language Processing',
    title: 'Sort support tickets',
    imports: 'scikit-learn',
    code: `from sklearn import linear_model
from sklearn.feature_extraction import text

tickets = [
    "refund my order", "I want my money back", "charged twice, please refund",
    "app crashes on start", "error when I log in", "the screen freezes after the update",
]
topics = ["billing", "billing", "billing", "bug", "bug", "bug"]

vectorizer = text.TfidfVectorizer()
model = linear_model.LogisticRegression().fit(vectorizer.fit_transform(tickets), topics)

for new in ["please refund the second charge", "login shows an error"]:
    print(f"{new!r} -> {model.predict(vectorizer.transform([new]))[0]}")
`,
  },
  {
    id: 'fruit',
    domain: 'Machine Learning',
    title: 'Apples or oranges',
    imports: 'scikit-learn, pandas',
    code: `import numpy as np
import pandas as pd
from sklearn import naive_bayes, preprocessing

rng = np.random.default_rng(0)
apples = pd.DataFrame({"weight": rng.normal(150, 12, 40), "width": rng.normal(7.5, 0.4, 40)})
oranges = pd.DataFrame({"weight": rng.normal(200, 15, 40), "width": rng.normal(8.5, 0.4, 40)})
fruit = pd.concat([apples, oranges], ignore_index=True)
kind = ["apple"] * 40 + ["orange"] * 40

scaler = preprocessing.StandardScaler().fit(fruit)
model = naive_bayes.GaussianNB().fit(scaler.transform(fruit), kind)

new_fruit = pd.DataFrame({"weight": [160, 195], "width": [7.7, 8.6]})
print(model.predict(scaler.transform(new_fruit)))
`,
  },
  {
    id: 'communities',
    domain: 'Graph Processing',
    title: 'Find communities',
    imports: 'NetworkX',
    code: `import networkx as nx
from networkx.algorithms import community

# A famous social network: 34 members of a karate club.
graph = nx.karate_club_graph()
groups = community.greedy_modularity_communities(graph)

print(f"{graph.number_of_nodes()} people fall into {len(groups)} communities")
print("Community sizes:", sorted(len(group) for group in groups))
`,
  },
  {
    id: 'blobs',
    domain: 'Computer Vision',
    title: 'Count bright regions',
    imports: 'SciPy',
    code: `import numpy as np
from scipy import ndimage

rng = np.random.default_rng(3)
noise = rng.random((120, 120))

# Blur the noise, then keep the bright regions and count them.
smooth = ndimage.gaussian_filter(noise, sigma=5)
bright = smooth > smooth.mean() + smooth.std()
labels, count = ndimage.label(bright)
sizes = ndimage.sum(bright, labels, range(1, count + 1))

print(f"Bright regions: {count}")
print(f"Largest region: {int(sizes.max())} pixels")
`,
  },
];
