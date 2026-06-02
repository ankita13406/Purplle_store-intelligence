import pandas as pd
import json

# Read Excel
xl = pd.ExcelFile("data/Brigade_Road_-_Store_layout.xlsx")
print("Sheets:", xl.sheet_names)

# Inspect first sheet
df = xl.parse(xl.sheet_names[0])
print(df.head(20))