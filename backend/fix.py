with open("scripts/pre_warm.py", "r") as f:
    text = f.read()

text = text.replace("from app.services.normalisation import load_orders, load_settlements, load_bank_txns\n", "")

with open("scripts/pre_warm.py", "w") as f:
    f.write(text)
