import pandas as pd
from pathlib import Path

base_dir = Path(__file__).resolve().parent

data_dir = base_dir / "data"
output_dir = base_dir / "output"
output_dir.mkdir(exist_ok=True)

output_file = output_dir / "entreprises_belgique_numero_nom_adresse_split.xlsx"

enterprise_file = data_dir / "enterprise.csv"
denomination_file = data_dir / "denomination.csv"
address_file = data_dir / "address.csv"


# 1. Enterprise numbers
enterprise = pd.read_csv(
    enterprise_file,
    sep=",",
    dtype=str,
    usecols=["EnterpriseNumber"]
)

enterprise = enterprise.rename(columns={
    "EnterpriseNumber": "numero_entreprise"
})

enterprise["numero_entreprise"] = enterprise["numero_entreprise"].str.strip()


# 2. Enterprise names
denomination = pd.read_csv(
    denomination_file,
    sep=",",
    dtype=str,
    usecols=["EntityNumber", "Language", "TypeOfDenomination", "Denomination"]
)

denomination = denomination.rename(columns={
    "EntityNumber": "numero_entreprise",
    "Denomination": "nom_entreprise"
})

denomination["numero_entreprise"] = denomination["numero_entreprise"].str.strip()

# Keep one name per enterprise.
# Priority: official denomination first, then abbreviation, then commercial name.
denomination["name_priority"] = denomination["TypeOfDenomination"].map({
    "001": 1,
    "002": 2,
    "003": 3
}).fillna(9)

denomination = (
    denomination
    .sort_values(["numero_entreprise", "name_priority"])
    .drop_duplicates(subset=["numero_entreprise"], keep="first")
    [["numero_entreprise", "nom_entreprise"]]
)


# 3. Enterprise address
address = pd.read_csv(
    address_file,
    sep=",",
    dtype=str,
    usecols=[
        "EntityNumber",
        "StreetFR",
        "StreetNL",
        "HouseNumber",
        "Box",
        "Zipcode",
        "MunicipalityFR",
        "MunicipalityNL"
    ]
)

address = address.rename(columns={
    "EntityNumber": "numero_entreprise",
    "HouseNumber": "house_number",
    "Box": "box_number",
    "Zipcode": "postcode"
})

address["numero_entreprise"] = address["numero_entreprise"].str.strip()

# Prefer French street/city names, fallback to Dutch if French is empty.
address["street_name"] = address["StreetFR"].fillna(address["StreetNL"])
address["city_name"] = address["MunicipalityFR"].fillna(address["MunicipalityNL"])

address = address[[
    "numero_entreprise",
    "street_name",
    "house_number",
    "box_number",
    "postcode",
    "city_name"
]]

# Keep one address per enterprise.
address = address.drop_duplicates(subset=["numero_entreprise"], keep="first")


# 4. Merge
df = (
    enterprise
    .merge(denomination, on="numero_entreprise", how="left")
    .merge(address, on="numero_entreprise", how="left")
)


# 5. Final columns only
df = df[[
    "numero_entreprise",
    "nom_entreprise",
    "street_name",
    "house_number",
    "box_number",
    "postcode",
    "city_name"
]]

df = df.drop_duplicates(subset=["numero_entreprise"])
df = df.sort_values("numero_entreprise")


# 6. Export to Excel, split sheets if needed
max_rows = 1_000_000

with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
    for i in range(0, len(df), max_rows):
        df.iloc[i:i + max_rows].to_excel(
            writer,
            sheet_name=f"entreprises_{i // max_rows + 1}",
            index=False
        )

print(f"Done: {output_file}")
print(f"Rows exported: {len(df)}")