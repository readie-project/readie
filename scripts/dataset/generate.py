from openai import AzureOpenAI
from TreeParser import TreeParser
import json
from json import JSONDecodeError
import os

from dotenv import load_dotenv
load_dotenv()

system_prompt = """
You are an expert data synthesizer acting as a diverse set of AI coding agents. 

### CORE DIRECTIVE:
Simulate the code an AI agent would write for users ranging from novices to experts. Scripts should vary in complexity—some should be simple one-liners or basic scripts, while others should be comprehensive multi-step pipelines (e.g., combining preprocessing, feature engineering, model training, evaluation metrics, and visualization in a single script).

### VARIATION & NOVELTY:
- Styles: Vary between procedural scripts, object-oriented structures, and functional approaches.
- Conventions: Use different naming styles (snake_case, camelCase) and levels of abstraction.
- Anti-Repetition: Do not generate code that is identical or nearly identical to these previously generated topics. Even if the task is the same, you must change the approach, the libraries used, or the dataset/model pairing.

### ECOSYSTEM CONSTRAINTS:
1. No Documentation: No docstrings or explanatory comments.
2. Dataset Rule: If using an external dataset, include exactly one comment: `# DATASET USED: <kaggle_url>`. Use plausible Kaggle URLs.
3. Model Rule: Use `transformers` (Hugging Face) for state-of-the-art models. Use `scikit-learn` for traditional ML, including its neural network modules.
4. Complexity: Mix simple tasks with "End-to-End" workflows that include plotting (matplotlib/seaborn) and metrics.

### OUTPUT FORMAT:
Return ONLY a valid JSON object. No markdown formatting, no backticks, no preamble. 

{
  "requests": [
    {
      "task_name": "Unique task identifier",
      "category": "Name of the category",
      "code": "Full escaped python code string"
    }
  ]
}
"""

def get_previously_generated_topics():
    PREVIOUSLY_GENERATED_TOPICS = set() # Set of previously generated topics to avoid repetition

    with open("./dataset.json", 'r') as f:
        data = json.load(f)

    for i in data:
        PREVIOUSLY_GENERATED_TOPICS.add(i['task_name'])

    return ", ".join(PREVIOUSLY_GENERATED_TOPICS)

def generate(category, num_requests):
    user_prompt = f"""
    Generate {num_requests} Python code snippets for the category: {category}
    Do not generate code that is identical or nearly identical to these previously generated topics: {get_previously_generated_topics()}
    """

    client = AzureOpenAI(
    api_version="2025-03-01-preview",
    azure_endpoint=os.environ.get("AZURE_ENDPOINT"),
    api_key=os.environ.get("AZURE_API_KEY")
    )

    messages = [
        {"role": "system", "content": system_prompt },
        {"role": "user", "content": user_prompt },
    ]

    response = client.chat.completions.create(
        model=os.environ.get("AZURE_MODEL_NAME"),
        messages=messages
    )

    response_data = response.choices[0].message.content
    try:
        codes = json.loads(response_data)['requests']
        for item in codes:
            try:
                parser = TreeParser()
                data.append({**item, **parser.start(item['code'])})
                with open("./dataset.json", 'w') as f:
                  json.dump(data, f, indent=2)
            except Exception as e:
                print(f"Error processing code for task {item['code']}: {e}")
    except JSONDecodeError as e:
        print(f"Error decoding response {response_data}: {e}")

CATEGORY_OPTIONS = [
    "Exploratory Data Analysis",
    "Feature Engineering",
    "Data Preprocessing",
    "Data Science",
    "Machine Learning",
    "Natural Language Processing",
    "Model Inference",
    "Model Training",
    "Computer Vision",
    "Image Processing",
    "Time Series Analysis",
    "Recommender Systems",
    "Anomaly Detection",
    "ETL Pipelines",
    "Data Visualization",
    "Graph Processing"
    # Add more categories as needed
]

CATEGORY_OPTIONS = [
    "Exploratory Data Analysis",
    "Feature Engineering",
    "Data Preprocessing",
    "Data Science",
    "Machine Learning",
    "Natural Language Processing",
    "Model Inference",
    "Model Training",
    "Computer Vision",
    "Image Processing",
    "Time Series Analysis",
    "Recommender Systems",
    "Anomaly Detection",
    "ETL Pipelines",
    "Data Visualization",
    "Graph Processing"
    # Add more categories as needed
]
NUM_REQUESTS = 10 # Number of code snippets to generate in each batch
BATCHES_PER_CATEGORY = 10 # Number of batches to generate for each category

if __name__ == "__main__":
    for category in CATEGORY_OPTIONS:
        print(f"Generating code snippets for category: {category}")
        for i in range(1, BATCHES_PER_CATEGORY + 1):
            print(f"Batch {i}/{BATCHES_PER_CATEGORY} for category: {category}")
            generate(category, NUM_REQUESTS)
    