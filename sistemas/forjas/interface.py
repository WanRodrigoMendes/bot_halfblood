# ============================================================
# SISTEMA DE FORJA / GERADOR DE FICHA
# ============================================================

import unicodedata
import re

from .materiais import MATERIAIS, MISTURAS


# ============================================================
# NORMALIZAÇÃO DE TEXTO
# ============================================================

def normalizar(texto):
    """
    Converte o texto para minúsculas e remove acentos.

    Exemplos:

        "Alumínio" -> "aluminio"
        "Nióbio"   -> "niobio"
        "Carvão"   -> "carvao"

    Isso facilita a identificação automática dos materiais.
    """

    if not texto:
        return ""

    texto = texto.lower()

    texto = unicodedata.normalize(
        "NFD",
        texto
    )

    texto = "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    )

    return texto


# ============================================================
# IDENTIFICAÇÃO DOS MATERIAIS
# ============================================================

def identificar_materiais(descricao):
    texto = normalizar(descricao)
    ocorrencias = []

    for chave, dados in {**MATERIAIS, **MISTURAS}.items():
        nomes = dados.get("nomes", [dados["nome_exibicao"]])

        for nome in nomes:
            nome_normalizado = normalizar(nome)
            padrao = rf"(?<!\w){re.escape(nome_normalizado)}(?!\w)"

            for correspondencia in re.finditer(padrao, texto):
                ocorrencias.append(
                    (correspondencia.start(), correspondencia.end(), chave)
                )

    selecionadas = []
    materiais_encontrados = set()

    for inicio, fim, chave in sorted(
        ocorrencias,
        key=lambda ocorrencia: (
            -(ocorrencia[1] - ocorrencia[0]),
            ocorrencia[0],
        ),
    ):
        sobreposta = any(
            inicio < fim_existente and fim > inicio_existente
            for inicio_existente, fim_existente, _ in selecionadas
        )

        if not sobreposta and chave not in materiais_encontrados:
            selecionadas.append((inicio, fim, chave))
            materiais_encontrados.add(chave)

    return [
        chave
        for _, _, chave in sorted(selecionadas)
    ]

# ============================================================
# IDENTIFICAÇÃO DAS MISTURAS
# ============================================================

def identificar_misturas(materiais):
    """
    Verifica se os materiais encontrados formam alguma
    mistura ou liga cadastrada em MISTURAS.

    Exemplo:

        ferro + carvão
              ↓
             aço

    Retorna:

        materiais_resultantes
        misturas_encontradas
    """

    # Cópia para não alterar a lista original
    materiais_resultantes = list(materiais)

    misturas_encontradas = []

    # --------------------------------------------------------
    # Procura cada receita cadastrada
    # --------------------------------------------------------

    for mistura, dados in MISTURAS.items():

        componentes = dados.get(
            "materiais",
            set()
        )

        # ----------------------------------------------------
        # Compatibilidade com dois formatos:
        #
        # {"ferro", "carvão"}
        #
        # ou:
        #
        # {"ferro": 1, "carvão": 1}
        # ----------------------------------------------------

        if isinstance(componentes, dict):

            componentes_set = set(
                componentes.keys()
            )

        else:

            componentes_set = set(
                componentes
            )

        # ----------------------------------------------------
        # Verifica se todos os componentes estão presentes
        # ----------------------------------------------------

        if componentes_set.issubset(
            set(materiais_resultantes)
        ):

            misturas_encontradas.append(
                mistura
            )

            # ------------------------------------------------
            # Remove os componentes usados
            # ------------------------------------------------

            for componente in componentes_set:

                if componente in materiais_resultantes:

                    materiais_resultantes.remove(
                        componente
                    )

            # ------------------------------------------------
            # Adiciona o produto da mistura
            # ------------------------------------------------

            materiais_resultantes.append(
                mistura
            )

    return (
        materiais_resultantes,
        misturas_encontradas
    )


# ============================================================
# OBTÉM DADOS DE MATERIAL OU MISTURA
# ============================================================

def obter_dados_material(nome):
    """
    Procura um material primeiro em MATERIAIS e depois
    em MISTURAS.

    Isso permite que o sistema trabalhe com:

        ferro
        carvão
        aço
        bronze
        latão
        etc.
    """

    if nome in MATERIAIS:

        return MATERIAIS[nome]

    if nome in MISTURAS:

        return MISTURAS[nome]

    return None


# ============================================================
# NOME DE EXIBIÇÃO
# ============================================================

def nome_material(nome):
    """
    Retorna o nome bonito do material.
    """

    dados = obter_dados_material(nome)

    if dados:

        return dados.get(
            "nome_exibicao",
            nome.capitalize()
        )

    return nome.capitalize()


# ============================================================
# CÁLCULO DOS BÔNUS
# ============================================================

def calcular_bonus(materiais):
    """
    Calcula todos os bônus fornecidos pelos materiais.

    O cálculo acontece somente depois das misturas terem
    sido identificadas.

    Exemplo:

        Ferro + Carvão

    vira:

        Aço

    Portanto, os bônus individuais do ferro e do carvão
    não são aplicados novamente caso eles tenham sido
    consumidos pela mistura.
    """

    bonus = {

        # ----------------------------------------------------
        # Fabricação
        # ----------------------------------------------------

        "bonus_fabricacao": 0,

        # ----------------------------------------------------
        # Corrosão
        # ----------------------------------------------------

        "corrosao_reducao": 0,

        "resistencia_corrosao": 0,

        # ----------------------------------------------------
        # Resistências
        # ----------------------------------------------------

        "resistencia_fogo": 0,

        "resistencia_calor": 0,

        "resistencia_oxidacao": 0,

        # ----------------------------------------------------
        # Estrutura
        # ----------------------------------------------------

        "resistencia": 0,

        "durabilidade": 0,

        # ----------------------------------------------------
        # Danos
        # ----------------------------------------------------

        "dano_igneo": 0,

        "dano_eletrico": 0,

        "dano_termico": 0,

        "dano_contusao": 0,

        # ----------------------------------------------------
        # Projéteis
        # ----------------------------------------------------

        "bonus_projetil": 0,

        # ----------------------------------------------------
        # Canalização
        # ----------------------------------------------------

        "canalizacao_magica": 0,

        "canalizacao_terra": 0,

        "canalizacao_agua": 0,

        "canalizacao_fogo": 0,

        "canalizacao_vento": 0,

        "canalizacao_elemental": 0,

        # ----------------------------------------------------
        # Propriedades especiais
        # ----------------------------------------------------

        "inoxidavel": False
    }

    # ========================================================
    # APLICA OS EFEITOS DOS MATERIAIS
    # ========================================================

    for material in materiais:

        dados = obter_dados_material(
            material
        )

        if not dados:
            continue

        efeitos = dados.get(
            "efeitos",
            {}
        )

        # ----------------------------------------------------
        # Aplica cada efeito
        # ----------------------------------------------------

        for efeito, valor in efeitos.items():

            # ------------------------------------------------
            # Efeito booleano
            # ------------------------------------------------

            if efeito == "inoxidavel":

                if valor:
                    bonus["inoxidavel"] = True

                continue

            # ------------------------------------------------
            # Efeito numérico
            # ------------------------------------------------

            if efeito in bonus:

                bonus[efeito] += valor

    # ========================================================
    # INTERAÇÕES ESPECIAIS
    # ========================================================

    aplicar_interacoes(
        materiais,
        bonus
    )

    return bonus


# ============================================================
# INTERAÇÕES ENTRE MATERIAIS
# ============================================================

def aplicar_interacoes(materiais, bonus):
    """
    Regras especiais que dependem da combinação de
    materiais.

    Essas regras são separadas do cálculo principal para
    facilitar futuras expansões.
    """

    # ========================================================
    # FERRO + CALCÁRIO
    # ========================================================

    # Essa regra somente será aplicada se ambos continuarem
    # presentes como materiais individuais.

    if (
        "ferro" in materiais
        and
        "calcário" in materiais
    ):

        bonus["bonus_fabricacao"] += 1


# ============================================================
# RESUMO DOS MATERIAIS
# ============================================================

def gerar_resumo_materiais(materiais):
    """
    Converte uma lista de IDs de materiais em nomes legíveis.

    Exemplo:

        ["ferro", "carvão"]

    vira:

        "Ferro, Carvão"
    """

    if not materiais:

        return "Nenhum material identificado."

    nomes = []

    for material in materiais:

        nomes.append(
            nome_material(material)
        )

    return ", ".join(nomes)


# ============================================================
# RESUMO DAS MISTURAS
# ============================================================

def gerar_resumo_misturas(misturas):
    """
    Gera uma lista legível das misturas encontradas.
    """

    if not misturas:

        return "Nenhuma."

    nomes = []

    for mistura in misturas:

        nomes.append(
            nome_material(mistura)
        )

    return ", ".join(nomes)


# ============================================================
# DESCRIÇÃO TÉCNICA
# ============================================================

def gerar_descricao_tecnica(materiais, bonus):
    """
    Converte os bônus calculados em uma descrição técnica.
    """

    partes = []

    # ========================================================
    # FABRICAÇÃO
    # ========================================================

    if bonus["bonus_fabricacao"] != 0:

        valor = bonus["bonus_fabricacao"]

        sinal = "+" if valor > 0 else ""

        partes.append(
            f"Bonificação de fabricação: "
            f"{sinal}{valor}."
        )

    # ========================================================
    # CORROSÃO
    # ========================================================

    if bonus["corrosao_reducao"] != 0:

        partes.append(
            f"Redução dos efeitos corrosivos: "
            f"{bonus['corrosao_reducao']}%."
        )

    if bonus["resistencia_corrosao"] != 0:

        partes.append(
            f"Resistência à corrosão: "
            f"+{bonus['resistencia_corrosao']}%."
        )

    # ========================================================
    # RESISTÊNCIAS
    # ========================================================

    if bonus["resistencia_fogo"] != 0:

        partes.append(
            f"Resistência contra fogo: "
            f"+{bonus['resistencia_fogo']}."
        )

    if bonus["resistencia_calor"] != 0:

        partes.append(
            f"Resistência ao calor: "
            f"+{bonus['resistencia_calor']}."
        )

    if bonus["resistencia_oxidacao"] != 0:

        partes.append(
            f"Resistência contra oxidação: "
            f"+{bonus['resistencia_oxidacao']}."
        )

    # ========================================================
    # ESTRUTURA
    # ========================================================

    if bonus["resistencia"] != 0:

        partes.append(
            f"Resistência estrutural: "
            f"+{bonus['resistencia']}."
        )

    if bonus["durabilidade"] != 0:

        partes.append(
            f"Durabilidade: "
            f"+{bonus['durabilidade']}."
        )

    # ========================================================
    # DANOS
    # ========================================================

    if bonus["dano_igneo"] != 0:

        partes.append(
            f"Dano ígneo: "
            f"+{bonus['dano_igneo']}."
        )

    if bonus["dano_eletrico"] != 0:

        partes.append(
            f"Dano elétrico: "
            f"+{bonus['dano_eletrico']}."
        )

    if bonus["dano_termico"] != 0:

        partes.append(
            f"Dano térmico: "
            f"+{bonus['dano_termico']}."
        )

    if bonus["dano_contusao"] != 0:

        partes.append(
            f"Dano por contusão: "
            f"+{bonus['dano_contusao']}."
        )

    # ========================================================
    # PROJÉTEIS
    # ========================================================

    if bonus["bonus_projetil"] != 0:

        partes.append(
            f"Bonificação para projéteis: "
            f"+{bonus['bonus_projetil']}."
        )

    # ========================================================
    # CANALIZAÇÃO
    # ========================================================

    if bonus["canalizacao_magica"] != 0:

        partes.append(
            f"Canalização mágica: "
            f"+{bonus['canalizacao_magica']}."
        )

    if bonus["canalizacao_terra"] != 0:

        partes.append(
            f"Canalização de terra: "
            f"+{bonus['canalizacao_terra']}."
        )

    if bonus["canalizacao_agua"] != 0:

        partes.append(
            f"Canalização de água: "
            f"+{bonus['canalizacao_agua']}."
        )

    if bonus["canalizacao_fogo"] != 0:

        partes.append(
            f"Canalização de fogo: "
            f"+{bonus['canalizacao_fogo']}."
        )

    if bonus["canalizacao_vento"] != 0:

        partes.append(
            f"Canalização de vento: "
            f"+{bonus['canalizacao_vento']}."
        )

    if bonus["canalizacao_elemental"] != 0:

        partes.append(
            f"Canalização elemental: "
            f"+{bonus['canalizacao_elemental']}."
        )

    # ========================================================
    # PROPRIEDADES ESPECIAIS
    # ========================================================

    if bonus["inoxidavel"]:

        partes.append(
            "O item é completamente inoxidável."
        )

    # ========================================================
    # RESULTADO
    # ========================================================

    if not partes:

        return (
            "Nenhum bônus ou propriedade especial "
            "foi identificado."
        )

    return " ".join(partes)


# ============================================================
# DESCRIÇÃO SIMPLIFICADA
# ============================================================

def gerar_descricao_simplificada(bonus):
    """
    Converte os modificadores em efeitos diretos para o jogador.
    """

    efeitos = []

    if bonus["bonus_fabricacao"] != 0:
        efeitos.append(
            f"Bônus de fabricação {bonus['bonus_fabricacao']:+}"
        )

    if bonus["inoxidavel"] or bonus["corrosao_reducao"] >= 100:
        efeitos.append("Imunidade à corrosão")
    elif bonus["corrosao_reducao"] > 0:
        efeitos.append(
            f"Redução dos efeitos corrosivos em {bonus['corrosao_reducao']}%"
        )

    if bonus["resistencia_corrosao"] != 0:
        efeitos.append(
            f"Resistência à corrosão +{bonus['resistencia_corrosao']}%"
        )

    if bonus["resistencia_fogo"] != 0:
        efeitos.append(
            f"Resistência ao fogo +{bonus['resistencia_fogo']}"
        )

    if bonus["resistencia_calor"] != 0:
        efeitos.append(
            f"Resistência ao calor +{bonus['resistencia_calor']}"
        )

    if bonus["resistencia_oxidacao"] != 0:
        efeitos.append(
            f"Resistência à oxidação +{bonus['resistencia_oxidacao']}"
        )

    if bonus["resistencia"] > 0 and bonus["durabilidade"] > 0:
        efeitos.append("Alta durabilidade")
    elif bonus["resistencia"] > 0:
        efeitos.append("Alta resistência")
    elif bonus["durabilidade"] > 0:
        efeitos.append("Alta durabilidade")

    danos_elementais = (
        ("dano_igneo", "com golpes de fogo"),
        ("dano_eletrico", "com golpes elétricos"),
        ("dano_termico", "com golpes térmicos"),
    )
    for chave, tipo_golpe in danos_elementais:
        dano = bonus[chave]
        if dano:
            dano += bonus["canalizacao_elemental"]
            efeitos.append(f"+{dano} de dano {tipo_golpe}")

    if bonus["dano_contusao"] != 0:
        efeitos.append(
            f"+{bonus['dano_contusao']} de dano com golpes de contusão"
        )

    if bonus["bonus_projetil"] != 0:
        efeitos.append(f"+{bonus['bonus_projetil']} de dano com projéteis")

    if bonus["canalizacao_magica"] != 0:
        efeitos.append(f"Canalização mágica +{bonus['canalizacao_magica']}")

    for chave, elemento in (
        ("canalizacao_terra", "terra"),
        ("canalizacao_agua", "água"),
        ("canalizacao_fogo", "fogo"),
        ("canalizacao_vento", "vento"),
    ):
        if bonus[chave] != 0:
            efeitos.append(f"Canalização de {elemento} +{bonus[chave]}")

    if bonus["canalizacao_elemental"] and not any(
        bonus[chave] for chave, _ in danos_elementais
    ):
        efeitos.append(f"Canalização elemental +{bonus['canalizacao_elemental']}")

    if not efeitos:
        return "Nenhum bônus."

    return f"({'; '.join(efeitos)})"


# ============================================================
# GERAÇÃO DA FICHA
# ============================================================

def gerar_ficha(descricao):

    # ========================================================
    # 1. IDENTIFICA OS MATERIAIS
    # ========================================================

    materiais = identificar_materiais(
        descricao
    )

    (
        materiais_finais,
        misturas
    ) = identificar_misturas(
        materiais
    )

    bonus = calcular_bonus(
        materiais_finais
    )

    # ========================================================
    # 4. GERA OS TEXTOS
    # ========================================================

    materiais_formatados = gerar_resumo_materiais(
        materiais
    )

    materiais_finais_formatados = gerar_resumo_materiais(
        materiais_finais
    )

    misturas_formatadas = gerar_resumo_misturas(
        misturas
    )

    descricao_tecnica = gerar_descricao_tecnica(
        materiais_finais,
        bonus
    )

    # ========================================================
    # 5. GERA A FICHA
    # ========================================================

    ficha = f"""
    ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
    
    -# ㅤ ⤷ *Composição resultante:* {materiais_finais_formatados}
    -# ㅤ ⤷ *Misturas/Ligas identificadas:* {misturas_formatadas}
    -# ㅤ ⤷ *Matéria prima:* {materiais_formatados}
    -# ㅤ ⤷ *Descrição técnica:* {descricao_tecnica}

    ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
    """

    return ficha


if __name__ == "__main__":

    descricao = """
> **Tramontinerangue:** um bumerangue um tanto peculiar, composto de 3 lâminas de aço, sendo uma delas "retrátil". Seu centro foi feito para balancear e equilibrar a peça, onde 3 espécies de argolas de aço sustentam todo o aparato do item. Quando colocado deitado sobre uma superfície, a argola superior possui um mecanismo simples onde apenas rodando como uma tampa de garrafa, a terceira lâmina aparece se "desprendendo" da segunda, não de seu interior mas como se uma lâmina se tornasse duas. A argola inferior permanece imóvel sem alterações externas, em contrapartida a argola do centro possui um imã interno de onde com o movimento do bumerangue jogado em pleno ar, é capaz de rotacionar em seu interior e gerar um campo eletromagnético pela força mecânica, basicamente quase como um dínamo interno, essa alteração nas linhas do campo geram uma corrente elétrica que é transmitida por pequenos fios de cobre internos da estrutura, conduzindo a eletricidade para as asas do bumerangue, claro... seu interior tem estes fios revestido com carvão para servir de resistor natural, convertendo a eletricidade em calor no interior da lâmina, que por consequência se espalha e possui maior concentração no fio das asas de onde se ganha um maior poder de corte e se torna capaz de causar queimaduras, a depender do ambiente externo quando em contato com um material inflamável, pode produzir fogo. (+? em perícia, 1d4 de dano de queimadura)
"""
    ficha = gerar_ficha(
        descricao=descricao
    )

    print(ficha)