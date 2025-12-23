import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
import numpy as np 

# --- Configurações Iniciais ---

SHEET_ID = os.getenv("SHEET_ID")
PRIVATE_KEY = os.getenv("GCP_SA_PRIVATE_KEY", "").replace("\\n", "\n")
CLIENT_EMAIL = os.getenv("GCP_SA_CLIENT_EMAIL")

# --- Funções de Conexão e Caching ---

@st.cache_resource
def get_service_account_credentials():
    """
    Constrói o JSON de credenciais a partir das variáveis de ambiente.
    """
    if not all([CLIENT_EMAIL, PRIVATE_KEY]):
        st.error("Erro de configuração: Credenciais do Google Cloud não encontradas.")
        st.stop()
        
    creds_info = {
        "type": os.getenv("GCP_SA_TYPE"),
        "project_id": os.getenv("GCP_SA_PROJECT_ID"),
        "private_key_id": os.getenv("GCP_SA_PRIVATE_KEY_ID"),
        "private_key": PRIVATE_KEY,
        "client_email": CLIENT_EMAIL,
        "client_id": os.getenv("GCP_SA_CLIENT_ID"),
        "auth_uri": os.getenv("GCP_SA_AUTH_URI"),
        "token_uri": os.getenv("GCP_SA_TOKEN_URI"), 
        "auth_provider_x509_cert_url": os.getenv("GCP_SA_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.getenv("GCP_SA_CLIENT_X509_CERT_URL"),
        "universe_domain": os.getenv("GCP_SA_UNIVERSE_DOMAIN")
    }
    
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, scope)
    return creds

@st.cache_data(ttl=600)
def load_data_from_gsheets(sheet_name):
    """Conecta ao Google Sheets e carrega os dados de uma aba específica."""
    try:
        creds = get_service_account_credentials()
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(sheet_name)
        
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        # Converte nomes de colunas para maiúsculas e remove espaços
        df.columns = [col.upper().strip() for col in df.columns]
        
        return df
    
    except Exception as e:
        st.error(f"Erro ao carregar dados da aba {sheet_name}. Verifique se o e-mail da Service Account tem acesso à planilha. Detalhes: {e}")
        st.stop()

# --- Funções de Processamento de Dados (Calculo de Custo de Insumos) ---

def sanitize_and_convert(df, column_name):
    """Limpa e converte colunas de valores para float."""
    if column_name not in df.columns:
        return df 
        
    df[column_name] = df[column_name].astype(str).str.replace('R$', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.strip()
    df[column_name] = pd.to_numeric(df[column_name], errors='coerce').fillna(0.0)
    return df

def calculate_master_ingredient_cost(df_ingredientes):
    """Calcula o custo unitário (por G, ML ou UN) de cada ingrediente mestre."""
    
    df = df_ingredientes.copy()
    df = sanitize_and_convert(df, 'VALOR_PACOTE')
    
    df['QUANT_PACOTE'] = pd.to_numeric(df['QUANT_PACOTE'], errors='coerce').fillna(1).replace(0, 1)
    
    df['CUSTO_UNITARIO'] = df['VALOR_PACOTE'] / df['QUANT_PACOTE']
    
    df = df[['NOME_ITEM', 'UNIDADE_PACOTE', 'CUSTO_UNITARIO']]
    df.columns = ['NOME_INGREDIENTE', 'UNIDADE_BASE', 'CUSTO_UNITARIO']
    
    custo_dict = df.set_index('NOME_INGREDIENTE')['CUSTO_UNITARIO'].to_dict()
    unidade_dict = df.set_index('NOME_INGREDIENTE')['UNIDADE_BASE'].to_dict()
    
    return custo_dict, unidade_dict

def calculate_recipe_cost(df_receitas, custo_dict, receita_col_name):
    """Calcula o custo total de uma base ou receita final, e retorna o detalhe."""
    
    df = df_receitas.copy()
    df['QUANT_RECEITA'] = pd.to_numeric(df['QUANT_RECEITA'], errors='coerce').fillna(0)

    def calc_ingrediente_custo(row):
        nome_ingrediente = row['NOME_INGREDIENTE']
        quantidade_receita = row['QUANT_RECEITA']
        custo_unitario = custo_dict.get(nome_ingrediente)
        
        if custo_unitario is None:
            return 0.0
        
        return custo_unitario * quantidade_receita

    df['CUSTO_UNITARIO'] = df['NOME_INGREDIENTE'].apply(lambda x: custo_dict.get(x, 0.0))
    df['CUSTO_ITEM'] = df.apply(calc_ingrediente_custo, axis=1)

    custo_total_receita_dict = df.groupby(receita_col_name)['CUSTO_ITEM'].sum().to_dict()
    
    return custo_total_receita_dict, df 

@st.cache_data(ttl=600)
def get_all_calculated_data():
    """Carrega todos os dados, calcula os custos intermediários e finais, e adiciona o preço de venda de mercado."""
    
    # 1. Carregar Dados de Receitas
    df_ingredientes = load_data_from_gsheets('ingredientes_mestres')
    df_bases = load_data_from_gsheets('receitas_bases')
    df_finais = load_data_from_gsheets('receitas_finais')
    
    # 2. Carregar a Tabela de Preços de Mercado
    df_precos_mercado_bruto = load_data_from_gsheets('tabela_precos_mercado')
    
    COL_PRODUTO_KEY = 'PRODUTO'
    
    if COL_PRODUTO_KEY not in df_precos_mercado_bruto.columns:
        st.error(f"Coluna principal '{COL_PRODUTO_KEY}' não encontrada na aba 'tabela_precos_mercado'. Nomes das colunas carregadas: {df_precos_mercado_bruto.columns.tolist()}. Verifique se a 1ª coluna se chama 'PRODUTO' e não tem caracteres ocultos.")
        st.stop()
        
    # Assume que a segunda coluna é o preço de venda (ignora o nome original como CUSTO_FIXO_OPERACIONAL)
    colunas_disponiveis = df_precos_mercado_bruto.columns.tolist()
    if len(colunas_disponiveis) < 2:
        st.error("A aba 'tabela_precos_mercado' deve ter pelo menos duas colunas (PRODUTO e PREÇO DE VENDA).")
        st.stop()
        
    COL_PRECO_KEY = colunas_disponiveis[1]

    # Cria o DF de preço usando a 1ª e 2ª coluna.
    df_precos_mercado = df_precos_mercado_bruto[[COL_PRODUTO_KEY, COL_PRECO_KEY]].copy()
    
    # Renomeia para o nome padronizado para o merge
    df_precos_mercado.rename(columns={COL_PRECO_KEY: 'PRECO_VENDA_FINAL', COL_PRODUTO_KEY: 'PRODUTO'}, inplace=True)
    df_precos_mercado = sanitize_and_convert(df_precos_mercado, 'PRECO_VENDA_FINAL')
    
    # 3. Calcular Custos
    custo_ingredientes_dict, unidade_ingredientes_dict = calculate_master_ingredient_cost(df_ingredientes)
    custo_bases_dict, df_bases_detalhe = calculate_recipe_cost(df_bases, custo_ingredientes_dict, receita_col_name='NOME_BASE')
    
    df_rendimento = df_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates()
    df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1).replace(0, 1)
    rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
    
    custo_bases_ajustado_dict = {}
    for base, custo in custo_bases_dict.items():
        rendimento = rendimento_bases.get(base, 1)
        custo_bases_ajustado_dict[base] = custo / rendimento
        
    custo_total_dict = {**custo_ingredientes_dict, **custo_bases_ajustado_dict}
    custo_finais_dict, df_finais_detalhe = calculate_recipe_cost(df_finais, custo_total_dict, receita_col_name='NOME_BOLO')
    
    # 4. Compilar o DataFrame FINAL
    df_receitas_finais = pd.DataFrame(custo_finais_dict.items(), columns=['Produto', 'Custo Total de Insumos (R$)'])
    df_receitas_finais['Tipo'] = 'Bolo Final (Especial)'

    df_bases_precificacao = pd.DataFrame(custo_bases_ajustado_dict.items(), columns=['Produto', 'Custo Total de Insumos (R$)'])
    df_bases_precificacao['Tipo'] = 'Bolo Comum (Base)'

    df_precificacao_completa = pd.concat([df_receitas_finais, df_bases_precificacao], ignore_index=True)
    
    # FIX CRÍTICO DE CASE SENSITIVITY: Renomear a coluna 'Produto' (P maiúsculo) para 'PRODUTO' (tudo maiúsculo) para o merge funcionar
    df_precificacao_completa.rename(columns={'Produto': 'PRODUTO'}, inplace=True) 
    
    df_precificacao_completa['Custo Total de Insumos (R$)'] = df_precificacao_completa['Custo Total de Insumos (R$)'].round(2)
    
    # 5. Merge com os preços de venda fixos
    df_precificacao_completa = pd.merge(
        df_precificacao_completa, 
        df_precos_mercado[['PRODUTO', 'PRECO_VENDA_FINAL']], 
        on='PRODUTO', 
        how='left'
    )
    
    # Renomeia a coluna e trata NaN (produtos sem preço definido)
    df_precificacao_completa.rename(columns={'PRECO_VENDA_FINAL': 'Preço de Venda (Mercado) (R$)'}, inplace=True)
    df_precificacao_completa['Preço de Venda (Mercado) (R$)'] = df_precificacao_completa['Preço de Venda (Mercado) (R$)'].fillna(0.0)
    
    # 6. Calcular o Lucro Bruto (R$) e a Margem Percentual
    
    # Evita divisão por zero (substitui 0 por NaN para não dividir)
    df_precificacao_completa['Custo Total de Insumos (R$)'] = df_precificacao_completa['Custo Total de Insumos (R$)'].replace(0, np.nan) 
    
    # 6a. Lucro Bruto (R$)
    df_precificacao_completa['Lucro Bruto (R$)'] = (
        df_precificacao_completa['Preço de Venda (Mercado) (R$)'] - df_precificacao_completa['Custo Total de Insumos (R$)']
    )
    
    # 6b. Margem Bruta (%) - Mantida para a tabela e o detalhe final
    df_precificacao_completa['Margem Bruta (%)'] = (
        df_precificacao_completa['Lucro Bruto (R$)'] / 
        df_precificacao_completa['Preço de Venda (Mercado) (R$)']
    ) * 100
    
    
    # TRATAMENTO DE ERROS DE CÁLCULO (Infinito, NaN) - GARANTE FLOAT PURO
    for col in ['Lucro Bruto (R$)', 'Margem Bruta (%)']:
        # Substitui Infinito e -Infinito por NaN 
        df_precificacao_completa[col] = df_precificacao_completa[col].replace([np.inf, -np.inf], np.nan)
        # Preenche os NaN resultantes (e os originais) com 0.0
        df_precificacao_completa[col] = df_precificacao_completa[col].fillna(0.0)

    df_precificacao_completa['Lucro Bruto (R$)'] = df_precificacao_completa['Lucro Bruto (R$)'].round(2) 
    df_precificacao_completa['Margem Bruta (%)'] = df_precificacao_completa['Margem Bruta (%)'].round(1)

    # Ordenação final
    df_precificacao_completa = df_precificacao_completa.sort_values(by='Preço de Venda (Mercado) (R$)', ascending=False)
    
    return df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases

# --- Streamlit App (Frontend) ---

def display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases):
    """Mostra o detalhe completo da receita e custo do produto final ou da base."""
    
    product_info = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]
    product_type = product_info['Tipo']
    
    st.subheader(f"Composição e Custo de Insumos: {selected_product}")
    st.caption(f"Tipo de Produto: **{product_type}**")

    if 'Bolo Final' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto é composto por Insumos Mestres e, possivelmente, Receitas Base (massas/coberturas).")
        
        df_bolo = df_finais_detalhe[df_finais_detalhe['NOME_BOLO'] == selected_product].copy()
        
        df_bolo['Tipo de Item'] = df_bolo['NOME_INGREDIENTE'].apply(
            lambda x: 'Base' if x in rendimento_bases else 'Ingrediente Mestre/Final'
        )
        df_bolo['Custo Total (R$)'] = df_bolo['CUSTO_ITEM'].round(4)
        df_bolo['Custo Unitário'] = df_bolo['CUSTO_UNITARIO'].round(4)
        
        df_display = df_bolo[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Tipo de Item', 'CUSTO_UNITARIO', 'Custo Total (R$)']]
        df_display.columns = ['Item/Base Usada', 'Qtd na Receita', 'Tipo', 'Custo/Unidade Base (R$)', 'Custo Total do Item (R$)']
        
        st.dataframe(df_display, hide_index=True, use_container_width=True)
        
        total_custo = df_display['Custo Total do Item (R$)'].sum()
        st.metric("Custo Total de Insumos", f"R$ {total_custo:,.2f}")
        
        bases_usadas = df_bolo[df_bolo['Tipo de Item'] == 'Base']['NOME_INGREDIENTE'].unique()
        
        if bases_usadas.size > 0:
            st.markdown("---")
            st.warning("🔎 **Rastreabilidade:** Detalhe dos custos de cada Base usada neste produto (rastreando até o ingrediente mestre).")
            
            for base in bases_usadas:
                st.markdown(f"#### Composição da Base: {base}")
                
                df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
                
                rendimento = rendimento_bases.get(base, 1)
                custo_base_ajustado = custo_total_dict.get(base, 0)
                
                st.caption(f"Custo total da produção da Base {base}: R$ {df_base['CUSTO_ITEM'].sum():,.2f}. Rendimento: {rendimento} Unidade(s).")
                st.caption(f"Custo Ajustado por UNIDADE de Base usada no produto final: R$ {custo_base_ajustado:,.4f}.")
                
                df_base['Custo Total (R$)'] = df_base['CUSTO_ITEM'].round(4)
                df_base['Custo/Unidade Mestre (R$)'] = df_base['CUSTO_UNITARIO'].round(4)

                df_base_display = df_base[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
                df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
                
                st.dataframe(df_base_display, hide_index=True, use_container_width=True)

    elif 'Bolo Comum' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto (massa pura) é composto **diretamente** por Insumos Mestres.")
        
        base = selected_product
        df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
        
        rendimento = rendimento_bases.get(base, 1)
        custo_base_ajustado = custo_total_dict.get(base, 0)
        
        st.caption(f"Custo total da produção da Base {base}: R$ {df_base['CUSTO_ITEM'].sum():,.2f}. Rendimento: {rendimento} Unidade(s).")
        st.caption(f"Custo Ajustado por UNIDADE (bolo/base) para o cálculo final: R$ {custo_base_ajustado:,.4f}.")
        
        df_base['Custo Total (R$)'] = df_base['CUSTO_ITEM'].round(4)
        df_base['Custo/Unidade Mestre (R$)'] = df_base['CUSTO_UNITARIO'].round(4)

        df_base_display = df_base[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
        df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
        
        st.dataframe(df_base_display, hide_index=True, use_container_width=True)
        
        total_custo = df_base_display['Custo Total na Base (R$)'].sum() / rendimento
        st.metric("Custo Total do Produto (Insumos)", f"R$ {total_custo:,.2f}")

def main():
    st.set_page_config(page_title="Caderno de Receitas e Análise de Margem de Lucro 🍰", layout="wide")
    st.title("Caderno de Receitas e Análise de Margem de Lucro")
    
    # --- 1. Carregar Dados ---
    with st.spinner('Ligando a IA da Precificação e buscando os dados no Sheets...'):
        try:
            df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases = get_all_calculated_data()
            all_products = df_precificacao_completa['PRODUTO'].tolist()
            
        except Exception as e:
            st.error(f"Não foi possível carregar ou calcular os dados. Verifique o checklist abaixo. Erro: {e}")
            
            # Checklist para ajudar a debuggar o Google Sheets
            st.markdown("---")
            st.subheader("Checklist de Conexão com o Google Sheets (Revisado)")
            st.error("""
            1. **Aba `tabela_precos_mercado` Existe?** O nome está EXATAMENTE assim?
            2. **Coluna 1 (Produto):** O nome da coluna no Sheets está EXATAMENTE como 'PRODUTO' ou 'Produto'? (Seu conteúdo deve bater com o das outras abas de receita)
            3. **Coluna 2 (Preço):** O preço de venda está na SEGUNDA coluna? O código agora ASSUME que a segunda coluna é o preço.
            4. **Permissão:** O e-mail da Service Account (GCP_SA_CLIENT_EMAIL) tem permissão de LEITURA na planilha?
            """)
            
            return
            
    st.success("Cálculos concluídos! Deslize para baixo ou comece sua consulta.")
    st.markdown("---")
    
    # --- 2. Interface de Consulta ---
    st.header("Análise de Preço e Margem")
    
    selected_product = st.selectbox(
        "Selecione o Produto para Análise Detalhada:",
        options=["Selecione um Produto..."] + sorted(all_products)
    )
    
    if selected_product == "Selecione um Produto...":
        st.info("Selecione um produto para comparar o custo dos insumos (Seu Custo) com o Preço de Venda (Seu Preço de Mercado).")
        
        st.subheader("Visão Geral de Lucro Bruto e Margem Percentual")
        
        # Tabela resumo
        df_display_summary = df_precificacao_completa[['PRODUTO', 'Tipo', 'Custo Total de Insumos (R$)', 'Preço de Venda (Mercado) (R$)', 'Lucro Bruto (R$)', 'Margem Bruta (%)']]
        df_display_summary.columns = ['Produto', 'Tipo', 'Custo Insumos (R$)', 'Preço de Venda (R$)', 'Lucro Bruto (R$)', 'Margem Bruta (%)']
        
        st.dataframe(df_display_summary, hide_index=True, use_container_width=True)
        return
        
    # Encontrou um produto
    else:
        tab1, tab2 = st.tabs(["📊 Análise de Lucro e Margem", "📋 Detalhe da Receita (Engenharia de Insumos)"])
        
        # --- TAB 1: CUSTO E PREÇO FINAL ---
        with tab1:
            product_data = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]
            
            custo_produto = product_data['Custo Total de Insumos (R$)']
            preco_venda = product_data['Preço de Venda (Mercado) (R$)']
            
            # TRATAMENTO DE TIPAGEM: Forçar para float
            try:
                lucro_bruto = float(product_data['Lucro Bruto (R$)'])
            except:
                lucro_bruto = 0.0
            
            try:
                margem_percentual = float(product_data['Margem Bruta (%)'])
            except:
                margem_percentual = 0.0

            # Três colunas para as métricas principais
            col1, col2, col3 = st.columns(3)
            col1.metric("Custo Total de Insumos (Seu Custo)", f"R$ {custo_produto:,.2f}")
            col2.metric("Preço de Venda (Seu Mercado)", f"R$ {preco_venda:,.2f}")
            
            # --- CÁLCULO DO LUCRO BRUTO EM R$ ---
            
            # Usando o Lucro Bruto (R$) como delta, o Streamlit automaticamente colore a flecha
            # (Verde se delta > 0, Vermelho se delta < 0, Cinza se delta = 0)
            col3.metric(
                label="Lucro Bruto (R$)", 
                value=f"R$ {lucro_bruto:,.2f}", 
                delta=lucro_bruto,
                delta_color='normal' # 'normal' é verde/vermelho padrão
            )
            
            # --- EXIBIÇÃO DA MARGEM PERCENTUAL (Separado para evitar o erro anterior) ---
            
            margem_color = '' 
            if margem_percentual > 40:
                margem_color = "🟢 **Excelente**"
            elif margem_percentual >= 20:
                margem_color = "🟡 **Razoável**"
            else:
                margem_color = "🔴 **Baixa**"
            
            st.markdown("---")
            st.markdown(f"#### Margem de Lucro Bruta: {margem_color}")
            st.subheader(f"**{margem_percentual:,.1f} %**")

            
            st.markdown("---")
            st.markdown(f"#### Detalhamento de Lucro e Margem")
            
            if preco_venda == 0.0:
                 st.error("🚨 **ALERTA DE DADOS:** Este produto não possui preço de venda definido na sua tabela de preços. O lucro não pode ser calculado.")
            else:
                st.info(f"""
                Você está utilizando o preço de venda de **R$ {preco_venda:,.2f}** para este produto, que tem um custo de insumos de **R$ {custo_produto:,.2f}**.
                
                #### 1. Lucro Bruto (Subtração Simples):
                """)
                st.latex(f"""
                    \text{{Lucro Bruto (R\$)}} = \text{{Preço de Venda}} - \text{{Custo Total}} = \text{{R\$ {preco_venda:,.2f}}} - \text{{R\$ {custo_produto:,.2f}}} = \mathbf{{\text{{R\$ {lucro_bruto:,.2f}}}}}
                """)
                
                st.info(f"""
                #### 2. Margem Bruta Percentual:
                """)
                st.latex(f"""
                    \text{{Margem Bruta (\\%)}} = \\frac{{\text{{Lucro Bruto}}}}{{\text{{Preço de Venda}}}} \times 100 = \mathbf{{ {margem_percentual:,.1f}\% }}
                """)
                
                st.info("""
                **(Lembrete LGPD: Seus dados estão sendo analisados apenas para fins de cálculo de custo e precificação. O foco no Lucro Bruto (R$) simplifica a interpretação e evita os erros de tipagem do Streamlit que ocorriam com a métrica percentual direta.)**
                """)

        # --- TAB 2: DETALHE DA RECEITA ---
        with tab2:
            display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases)

if __name__ == '__main__':
    main()
