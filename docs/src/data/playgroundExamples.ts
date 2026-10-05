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
    id: 'loans',
    domain: 'Machine Learning',
    title: 'Loan approvals',
    imports: 'scikit-learn, pandas, numpy, matplotlib',
    code: `import io

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

matplotlib.use("Agg")  # draw without a screen

# 300 past loans: the borrower's income (in hundred thousand dollars), debt as a share of
# income, and whether the loan was repaid.
rng = np.random.default_rng(11)
income = rng.normal(0.6, 0.2, 300).clip(0.15, 1.5)
debt = rng.normal(0.35, 0.12, 300).clip(0.05, 0.9)
repaid = (income - 1.5 * debt + rng.normal(0, 0.15, 300)) > -0.1
loans = pd.DataFrame({"income": income, "debt": debt, "repaid": repaid})

# Train on the first 240 loans and test on the 60 the model has not seen.
features, outcome = loans[["income", "debt"]], loans["repaid"]
model = LogisticRegression().fit(features[:240], outcome[:240])
print(f"Accuracy on 60 unseen loans: {model.score(features[240:], outcome[240:]):.0%}")

# A new applicant: $45,000 of income, with debt at 50% of it.
applicant = pd.DataFrame({"income": [0.45], "debt": [0.5]})
print(f"Chance of repaying: {model.predict_proba(applicant)[0, 1]:.0%}")

# Draw the loans and the line where the model switches from "repaid" to "not repaid".
weight_income, weight_debt = model.coef_[0]
line_x = np.array([0.15, 1.5])
line_y = -(weight_income * line_x + model.intercept_[0]) / weight_debt
fig, ax = plt.subplots(figsize=(5, 4))
ax.scatter(features["income"], features["debt"], c=outcome, cmap="coolwarm", s=12)
ax.plot(line_x, line_y, "k--")
ax.set_ylim(0, 0.9)
ax.set_xlabel("income")
ax.set_ylabel("debt share")
chart = io.BytesIO()
fig.savefig(chart, format="png")
print(f"Chart with the decision line: {len(chart.getvalue()) / 1024:.0f} KB")
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
    id: 'features',
    domain: 'Data Preprocessing',
    title: 'Prepare data for a model',
    imports: 'scikit-learn, pandas',
    code: `import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler

houses = pd.DataFrame({
    "area": [50, 80, 120, 65],
    "rooms": [2, 3, 5, 3],
    "city": ["Austin", "Boston", "Austin", "Denver"],
})

# Put the numbers on one scale and turn the city into one column per value.
prepare = ColumnTransformer([
    ("numbers", StandardScaler(), ["area", "rooms"]),
    ("city", OneHotEncoder(), ["city"]),
])
table = pd.DataFrame(
    prepare.fit_transform(houses),
    columns=["area", "rooms", "Austin", "Boston", "Denver"],
)
print(table.round(2).to_string(index=False))
`,
  },
];
