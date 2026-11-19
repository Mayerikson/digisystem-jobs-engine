async def main() -> int:
    if not ARQUIVO_CSV.exists():
        print(f"ERRO: arquivo nao encontrado: {ARQUIVO_CSV}", file=sys.stderr)
        return 1

    df = pd.read_csv(ARQUIVO_CSV, encoding="utf-8-sig", dtype=str).fillna("")
    obrigatorias = {"ID", "Vaga", "Detalhes", "Link de Inscrição"}
    faltantes = obrigatorias - set(df.columns)
    if faltantes:
        print(f"ERRO: colunas ausentes: {sorted(faltantes)}", file=sys.stderr)
        return 1

    amostra = selecionar_amostra(df)
    if amostra.empty:
        print("ERRO: nenhuma vaga selecionada para verificacao.", file=sys.stderr)
        return 1

    # Acrescenta o caso reclamado sem depender de ele continuar na listagem.
    link_manual = VAGA_RECLAMADA["Link de Inscrição"]
    if not amostra["Link de Inscrição"].astype(str).eq(link_manual).any():
        amostra = pd.concat(
            [amostra, pd.DataFrame([VAGA_RECLAMADA])], ignore_index=True, sort=False
        )

    print(f"Total de vagas na coleta: {len(df)}")
    print(f"Vagas selecionadas para verificacao: {len(amostra)}")
    print("CASO MANUAL: Analista de Governanca de Dados (SPS0001946)")
    print(f"URL: {link_manual}")

    for _, vaga in amostra.iterrows():
        print(f"AMOSTRA: {vaga['ID']} | {vaga['Vaga']} | {vaga['Link de Inscrição']}")

    resultados = await verificar_disponibilidade_vagas(
        amostra.to_dict(orient="records")
    )
    resultado_df = pd.DataFrame(resultados)
    resultado_df.to_csv(ARQUIVO_RESULTADO_CSV, index=False, encoding="utf-8-sig")
    ARQUIVO_JSON.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    contagens = resultado_df["status_disponibilidade"].value_counts().to_dict()
    print("RESUMO DA VERIFICACAO:")
    print(json.dumps(contagens, ensure_ascii=False))
    manual = resultado_df[resultado_df["ID"] == VAGA_RECLAMADA["ID"]]
    if not manual.empty:
        registro = manual.iloc[0]
        print(
            "RESULTADO CASO MANUAL: "
            f"{registro['status_disponibilidade']} — {registro['motivo_verificacao']}"
        )
    print(f"Resultados salvos em {ARQUIVO_JSON} e {ARQUIVO_RESULTADO_CSV}.")
    print("ATENCAO: UNKNOWN nao significa vaga aberta; exige nova verificacao.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
