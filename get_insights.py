import pandas as pd
df = pd.read_json("data/profiles.jsonl", lines=True)
holdings = df[["isin", "name", "top_holdings"]].explode("top_holdings")
holdings = pd.concat([holdings.drop("top_holdings", axis=1), holdings["top_holdings"].apply(pd.Series)], axis=1)
holdings.to_excel("data/holdings.xlsx", index=False)