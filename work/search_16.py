with open("ondemand_js.js", "r", encoding="utf-8") as f:
    text = f.read()

import re
# Find all occurrences of 16
for m in re.finditer(r'\b16\b', text):
    start = max(0, m.start() - 40)
    end = min(len(text), m.end() + 40)
    print("Match 16:", text[start:end])
