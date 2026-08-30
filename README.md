# Pipoca & Play

Aplicação de recomendações de filmes baseada nas sete respostas do questionário.
O frontend preserva o HTML fornecido e exibe o banner/pôster de cada catálogo via
Wikipedia depois que a recomendação é retornada.

## Executar localmente

1. Revogue a chave exposta anteriormente no painel da OpenAI e crie uma nova.
2. Copie `.env.example` para `.env` e preencha `OPENAI_API_KEY`.
3. Execute `python3 server.py` dentro desta pasta.
4. Abra `http://127.0.0.1:8000`.

A chave é lida somente no servidor. O navegador envia apenas os sete filtros para
`POST /api/recommend`; o servidor chama a Responses API e devolve o JSON estruturado.

## Publicação na Vercel

O arquivo `vercel.json` e a função `api/recommend.py` já deixam este repositório
pronto para a Vercel. Importe o repositório no painel da Vercel e defina estas
variáveis de ambiente em **Project Settings → Environment Variables**:

- `OPENAI_API_KEY`: uma nova chave da OpenAI, nunca a chave exposta anteriormente;
- `OPENAI_MODEL`: `gpt-5.6-luna` (ou outro modelo disponível no seu projeto).

Não publique o arquivo `.env` e não coloque a chave no HTML. A Vercel serve
`public/index.html` como o site e executa `api/recommend.py` somente no servidor.
