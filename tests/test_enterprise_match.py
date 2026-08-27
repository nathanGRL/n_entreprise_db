from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enterprise_match import (  # noqa: E402
    MatchConfig,
    ReferenceIndex,
    STATUS_CERTAIN,
    STATUS_NONE,
    STATUS_SKIPPED,
    build_index_from_raw_bce,
    build_index_from_reference_file,
    match_dataframe,
    is_valid_belgian_enterprise_number,
    normalize_company_name,
    normalize_enterprise_number,
    normalize_street,
    parse_full_address,
    street_core,
)


def build_reference(tmp_path: Path) -> Path:
    reference = pd.DataFrame(
        [
            {
                "numero_entreprise": "0123.456.789",
                "nom_entreprise": "Les Jardins de Bruxelles SRL",
                "street_name": "Rue de la Loi",
                "house_number": "42",
                "box_number": "3",
                "postcode": "1000",
                "city_name": "Bruxelles",
                "status": "AC",
            },
            {
                "numero_entreprise": "0123.456.789",
                "nom_entreprise": "Jardins Bruxelles",
                "street_name": "Rue de la Loi",
                "house_number": "42",
                "box_number": "3",
                "postcode": "1000",
                "city_name": "Bruxelles",
                "status": "AC",
            },
            {
                "numero_entreprise": "0123.456.780",
                "nom_entreprise": "Les Jardins de Bruxelles Nord SRL",
                "street_name": "Rue de la Loi",
                "house_number": "44",
                "box_number": "",
                "postcode": "1000",
                "city_name": "Bruxelles",
                "status": "AC",
            },
            {
                "numero_entreprise": "0234.567.890",
                "nom_entreprise": "Garage Dupont SA",
                "street_name": "Rue des Cerisiers",
                "house_number": "14",
                "box_number": "",
                "postcode": "5030",
                "city_name": "Gembloux",
                "status": "AC",
            },
            {
                "numero_entreprise": "0567.890.123",
                "nom_entreprise": "Alpha Consulting SRL",
                "street_name": "Avenue Louise",
                "house_number": "100",
                "box_number": "",
                "postcode": "1050",
                "city_name": "Ixelles",
                "status": "AC",
            },
            {
                "numero_entreprise": "0678.901.234",
                "nom_entreprise": "Alpha Consulting BV",
                "street_name": "Avenue Louise",
                "house_number": "102",
                "box_number": "",
                "postcode": "1050",
                "city_name": "Ixelles",
                "status": "AC",
            },
        ]
    )
    reference_path = tmp_path / "reference.csv"
    reference.to_csv(reference_path, index=False)
    index_path = tmp_path / "reference.sqlite"
    build_index_from_reference_file(
        reference_path=reference_path,
        index_path=index_path,
        overwrite=True,
        column_map={},
        chunk_size=100,
        cache_mb=32,
        encoding=None,
        delimiter=None,
    )
    return index_path


def test_normalisation_helpers() -> None:
    assert normalize_company_name("Les Jardins de Bruxelles S.R.L.") == "les jardins de bruxelles"
    assert normalize_company_name("ALPHA CONSULTING B.V.") == "alpha consulting"
    assert normalize_company_name("I.D.E.A. S.C") == "idea"
    assert street_core(normalize_street("Stropstraat")) == street_core(normalize_street("Strop straat"))
    assert normalize_enterprise_number("BE 123 456 789") == "0123.456.789"
    assert is_valid_belgian_enterprise_number("0200.065.765")
    assert not is_valid_belgian_enterprise_number("0123.456.789")
    parsed = parse_full_address("Rue de la Loi 42 boîte 3, 1000 Bruxelles")
    assert parsed == {
        "street": "Rue de la Loi",
        "house_number": "42",
        "box": "3",
        "postcode": "1000",
        "city": "Bruxelles",
    }


def test_end_to_end_matching_and_ambiguity(tmp_path: Path) -> None:
    index_path = build_reference(tmp_path)
    iraiser = pd.DataFrame(
        [
            {
                "reference": "A1",
                "company_name": "Les jardin Bruxelles",
                "street3": "rue de la loi",
                "street_number": "42",
                "street_box": "bte 3",
                "zip": "1000",
                "city": "BRUXELLES",
                "enterprise_number": "",
            },
            {
                "reference": "A2",
                "company_name": "Garage Jean Dupont",
                "street3": "rue des cerisiers",
                "street_number": "14",
                "street_box": "",
                "zip": "5030",
                "city": "Gembloux",
                "enterprise_number": "",
            },
            {
                "reference": "A3",
                "company_name": "Alpha Consulting",
                "street3": "Avenue Louise",
                "street_number": "",
                "street_box": "",
                "zip": "1050",
                "city": "Ixelles",
                "enterprise_number": "",
            },
            {
                "reference": "A4",
                "company_name": "Entreprise inconnue",
                "street3": "Rue Imaginaire",
                "street_number": "99",
                "street_box": "",
                "zip": "9999",
                "city": "Nullepart",
                "enterprise_number": "",
            },
            {
                "reference": "A5",
                "company_name": "Garage Dupont",
                "street3": "Rue des Cerisiers",
                "street_number": "14",
                "street_box": "",
                "zip": "5030",
                "city": "Gembloux",
                "enterprise_number": "0200.065.765",
            },
            {
                "reference": "A6",
                "company_name": "Garage Dupont",
                "street3": "Rue des Cerisiers",
                "street_number": "14",
                "street_box": "",
                "zip": "5030",
                "city": "Gembloux",
                "enterprise_number": "0123.456.789",
            },
        ]
    )

    with ReferenceIndex(index_path, MatchConfig()) as index:
        result_rows, _, _, _ = match_dataframe(
            iraiser,
            index=index,
            column_map={},
            only_missing=True,
            progress_every=0,
        )

    by_id = {row["source_id"]: row for row in result_rows}
    assert by_id["A1"]["match_status"] == STATUS_CERTAIN
    assert by_id["A1"]["automatic_enterprise_number"] == "0123.456.789"
    assert by_id["A2"]["match_status"] == STATUS_CERTAIN
    assert by_id["A2"]["automatic_enterprise_number"] == "0234.567.890"

    # Same normalised name, same street/postcode/city, but two possible house
    # numbers and no house number in the input: never auto-assign.
    assert by_id["A3"]["match_status"] == STATUS_NONE
    assert by_id["A3"]["automatic_enterprise_number"] == ""
    assert by_id["A3"]["score_gap"] == 0.0

    assert by_id["A4"]["match_status"] == STATUS_NONE
    assert by_id["A5"]["match_status"] == STATUS_SKIPPED
    assert by_id["A6"]["match_status"] == STATUS_CERTAIN
    assert by_id["A6"]["existing_enterprise_number_valid"] is False


def test_raw_bce_index_can_match_establishment_address(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pd.DataFrame(
        [
            {
                "EnterpriseNumber": "0403.449.823",
                "Status": "AC",
                "JuridicalSituation": "000",
                "TypeOfEnterprise": "2",
                "JuridicalForm": "116",
                "JuridicalFormCAC": "",
                "StartDate": "01-01-1970",
            }
        ]
    ).to_csv(data_dir / "enterprise.csv", index=False)
    pd.DataFrame(
        [
            {
                "EntityNumber": "0403.449.823",
                "Language": "2",
                "TypeOfDenomination": "001",
                "Denomination": "Example Belgium SRL",
            },
            {
                "EntityNumber": "0403.449.823",
                "Language": "2",
                "TypeOfDenomination": "002",
                "Denomination": "Example",
            },
        ]
    ).to_csv(data_dir / "denomination.csv", index=False)
    pd.DataFrame(
        [
            {
                "EstablishmentNumber": "2.000.000.339",
                "StartDate": "01-11-1974",
                "EnterpriseNumber": "0403.449.823",
            }
        ]
    ).to_csv(data_dir / "establishment.csv", index=False)
    pd.DataFrame(
        [
            {
                "EntityNumber": "0403.449.823",
                "TypeOfAddress": "REGO",
                "CountryNL": "België",
                "CountryFR": "Belgique",
                "Zipcode": "1000",
                "MunicipalityNL": "Brussel",
                "MunicipalityFR": "Bruxelles",
                "StreetNL": "Wetstraat",
                "StreetFR": "Rue de la Loi",
                "HouseNumber": "10",
                "Box": "",
                "ExtraAddressInfo": "",
                "DateStrikingOff": "",
            },
            {
                "EntityNumber": "2.000.000.339",
                "TypeOfAddress": "BAET",
                "CountryNL": "België",
                "CountryFR": "Belgique",
                "Zipcode": "5030",
                "MunicipalityNL": "Gembloers",
                "MunicipalityFR": "Gembloux",
                "StreetNL": "Kersenstraat",
                "StreetFR": "Rue des Cerisiers",
                "HouseNumber": "14",
                "Box": "",
                "ExtraAddressInfo": "",
                "DateStrikingOff": "",
            },
        ]
    ).to_csv(data_dir / "address.csv", index=False)

    index_path = tmp_path / "raw.sqlite"
    build_index_from_raw_bce(
        data_dir=data_dir,
        index_path=index_path,
        overwrite=True,
        include_establishments=True,
        include_historical=False,
        chunk_size=10,
        cache_mb=32,
    )

    iraiser = pd.DataFrame(
        [
            {
                "reference": "B1",
                "company_name": "Example Belgium",
                "street3": "Rue des Cerisiers",
                "street_number": "14",
                "street_box": "",
                "zip": "5030",
                "city": "Gembloux",
                "enterprise_number": "",
            }
        ]
    )
    with ReferenceIndex(index_path, MatchConfig()) as index:
        result_rows, _, _, _ = match_dataframe(
            iraiser,
            index=index,
            column_map={},
            only_missing=True,
            progress_every=0,
        )
    assert result_rows[0]["match_status"] == STATUS_CERTAIN
    assert result_rows[0]["automatic_enterprise_number"] == "0403.449.823"
    assert result_rows[0]["candidate_street_name"] == "Rue des Cerisiers"



def test_flattened_multisheet_excel_is_fully_indexed(tmp_path: Path) -> None:
    columns = [
        "numero_entreprise",
        "nom_entreprise",
        "street_name",
        "house_number",
        "box_number",
        "postcode",
        "city_name",
    ]
    first = pd.DataFrame(
        [["0111.111.111", "Première Société SRL", "Rue A", "1", "", "1000", "Bruxelles"]],
        columns=columns,
    )
    second = pd.DataFrame(
        [["0222.222.222", "Deuxième Société SA", "Rue B", "2", "", "5000", "Namur"]],
        columns=columns,
    )
    reference_path = tmp_path / "reference.xlsx"
    with pd.ExcelWriter(reference_path, engine="xlsxwriter") as writer:
        first.to_excel(writer, sheet_name="entreprises_1", index=False)
        second.to_excel(writer, sheet_name="entreprises_2", index=False)

    index_path = tmp_path / "reference.sqlite"
    build_index_from_reference_file(
        reference_path=reference_path,
        index_path=index_path,
        overwrite=True,
        column_map={},
        chunk_size=100,
        cache_mb=32,
        encoding=None,
        delimiter=None,
    )
    with ReferenceIndex(index_path, MatchConfig()) as index:
        count = index.connection.execute("SELECT COUNT(*) FROM enterprise").fetchone()[0]
    assert count == 2


def test_flattened_multisheet_excel_is_fully_indexed(tmp_path: Path) -> None:
    columns = [
        "numero_entreprise",
        "nom_entreprise",
        "street_name",
        "house_number",
        "box_number",
        "postcode",
        "city_name",
    ]
    first = pd.DataFrame(
        [["0111.111.111", "Première Société SRL", "Rue A", "1", "", "1000", "Bruxelles"]],
        columns=columns,
    )
    second = pd.DataFrame(
        [["0222.222.222", "Deuxième Société SA", "Rue B", "2", "", "5000", "Namur"]],
        columns=columns,
    )
    reference_path = tmp_path / "reference.xlsx"
    with pd.ExcelWriter(reference_path, engine="xlsxwriter") as writer:
        first.to_excel(writer, sheet_name="entreprises_1", index=False)
        second.to_excel(writer, sheet_name="entreprises_2", index=False)

    index_path = tmp_path / "reference.sqlite"
    build_index_from_reference_file(
        reference_path=reference_path,
        index_path=index_path,
        overwrite=True,
        column_map={},
        chunk_size=100,
        cache_mb=32,
        encoding=None,
        delimiter=None,
    )
    with ReferenceIndex(index_path, MatchConfig()) as index:
        count = index.connection.execute("SELECT COUNT(*) FROM enterprise").fetchone()[0]
    assert count == 2
