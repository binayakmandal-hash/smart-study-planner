import os
from syllabus_processor import process_syllabus

files = os.listdir("uploads")

print("Files found in uploads:")

for file in files:
    print(file)

pdf_files = [
    file for file in files
    if file.lower().endswith(".pdf")
]

if not pdf_files:
    print("No PDF found.")
    exit()

file_path = os.path.join(
    "uploads",
    pdf_files[-1]
)

print("\nReading:")
print(file_path)

result = process_syllabus(
    file_path,
    "pdf"
)

print("\n========== EXTRACTED TEXT ==========\n")

print(result["cleaned_text"])

print("\n========== SUBJECTS & TOPICS ==========\n")

for subject, subj_info in result["subjects"].items():
    print(f"\nSUBJECT: {subject} (Code: {subj_info.get('code', 'N/A')}, Total Topics: {subj_info.get('total_topics', 0)})")
    for topic in subj_info.get("topics", []):
        print(f"  - [{topic.get('unit_name')}] {topic.get('topic_name')} (Diff: {topic.get('difficulty_score')})")