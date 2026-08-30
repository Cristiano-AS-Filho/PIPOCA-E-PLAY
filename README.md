# Pipoca & Play

**Pipoca & Play** é uma plataforma SaaS de recomendação personalizada de filmes. O cliente cria seu acesso com e-mail e senha, aguarda a validação do administrador e, depois da liberação, responde aos sete filtros do MVP para receber exatamente três opções ordenadas por compatibilidade, com explicação curta e histórico local da sessão.

## Funcionalidades disponíveis

A experiência pública começa em uma **landing page** responsiva, com apresentação do produto e CTA de entrada. A autenticação usa sessão por cookie `HttpOnly`, `SameSite=Lax`, assinatura HMAC e expiração automática. O cadastro exige senha individual com hash PBKDF2, começa com status `pending` e pode ser acompanhado automaticamente pelo cliente. O papel `admin` abre um painel restrito para listar pedidos, aceitar, rejeitar ou excluir usuários.

A lógica oficial dos sete filtros foi preservada: gênero principal, humor/vibe do dia, tempo disponível, época do filme, plataforma de streaming, companhia e popularidade/estilo. O backend valida o JSON da IA e exige três recomendações ordenadas. A pontuação exibida é um **match próprio do sistema**, não uma nota de IMDb ou crítica.

O enriquecimento factual é separado da IA. Quando `TMDB_API_KEY` está configurada, o adaptador consulta posters, backdrops, duração, gêneros e disponibilidade no Brasil. Sem essa chave, o sistema informa explicitamente que a disponibilidade não foi confirmada e oferece links de conferência no JustWatch, IMDb e Letterboxd; o modelo nunca é tratado como banco de dados.

## Executar localmente

1. Copie `.env.example` para `.env`.
2. Mantenha `ENVIRONMENT` diferente de `production` para executar localmente. O admin de desenvolvimento usa `admin@pipocaplay.com` / `admin123`; troque esses valores antes de qualquer uso real.
3. Defina `AUTH_SECRET`, `ADMIN_EMAIL` e `ADMIN_PASSWORD` com valores privados e fortes. Não publique `.env`.
4. Em desenvolvimento, `USER_STORE_FILE=data/users.json` cria a base local automaticamente. Em produção serverless, configure `KV_REST_API_URL` e `KV_REST_API_TOKEN` com uma base Redis REST persistente.
5. Defina `OPENAI_API_KEY` e, se disponível, `TMDB_API_KEY`.
6. Execute `python3 server.py` dentro desta pasta.
7. Abra `http://127.0.0.1:8000`.

A chave da OpenAI é lida apenas pelo servidor. O navegador envia os sete filtros para `POST /api/recommend` apenas depois do login.

## Publicação na Vercel

O arquivo `vercel.json` e as funções em `api/` deixam o repositório pronto para a Vercel. Em **Project Settings → Environment Variables**, configure, por ambiente:

| Variável | Obrigatória | Uso |
| --- | --- | --- |
| `OPENAI_API_KEY` | Sim | Chave privada para gerar recomendações. |
| `OPENAI_MODEL` | Sim | Modelo disponível no projeto OpenAI. |
| `AUTH_SECRET` | Sim | Segredo longo e aleatório para assinar sessões. |
| `ADMIN_EMAIL` | Sim | E-mail do administrador. |
| `ADMIN_PASSWORD` | Sim | Senha privada do administrador. |
| `USER_STORE_FILE` | Local | Caminho do JSON local de contas; não é persistente entre execuções serverless. |
| `KV_REST_API_URL` | Produção | URL REST do Redis (Vercel KV/Upstash) para armazenar contas entre invocações. |
| `KV_REST_API_TOKEN` | Produção | Token privado do Redis REST. |
| `TMDB_API_KEY` | Não | Habilita enriquecimento de catálogo e disponibilidade no Brasil. |
| `ENVIRONMENT=production` | Recomendada | Desativa credenciais padrão de desenvolvimento e ativa cookies seguros. |

O cadastro de clientes é armazenado na base configurada e segue os estados `pending`, `approved` e `rejected`. Somente contas `approved` conseguem criar sessão e usar o motor de recomendação. O painel admin é protegido por sessão e bloqueia qualquer rota administrativa para usuários comuns. A solução inclui persistência local para desenvolvimento e integração Redis REST para o runtime serverless da Vercel; recuperação de senha, e-mail transacional e auditoria multi-admin permanecem como evoluções futuras.

Não coloque chaves no HTML, no Git ou em mensagens de erro. Se uma chave tiver sido exposta anteriormente, revogue-a no respectivo provedor antes de publicar.
