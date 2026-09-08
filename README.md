# Pipoca & Play

**Pipoca & Play** é uma plataforma SaaS de recomendação personalizada de filmes. O cliente cria seu acesso com e-mail e senha, aguarda a validação do administrador, assina um dos três planos e, com o pagamento confirmado, responde aos sete filtros do MVP para receber exatamente três opções ordenadas por compatibilidade, com explicação curta. Cada consulta consome um crédito diário, e o que o cliente marca como “já assisti”, “gostei” ou “não gostei” passa a orientar as próximas indicações.

## Funcionalidades disponíveis

A experiência pública começa em uma **landing page** responsiva, com apresentação do produto e CTA de entrada. A autenticação usa sessão por cookie `HttpOnly`, `SameSite=Lax`, assinatura HMAC e expiração automática. O cadastro exige senha individual com hash PBKDF2, começa com status `pending` e pode ser acompanhado automaticamente pelo cliente. O papel `admin` abre um painel restrito para listar pedidos, aceitar, rejeitar ou excluir usuários.

A lógica oficial dos sete filtros foi preservada: gênero principal, humor/vibe do dia, tempo disponível, época do filme, plataforma de streaming, companhia e popularidade/estilo. O backend valida o JSON da IA e exige três recomendações ordenadas. A pontuação exibida é um **match próprio do sistema**, não uma nota de IMDb ou crítica.

O enriquecimento factual é separado da IA. Quando `TMDB_API_KEY` está configurada, o adaptador consulta posters, backdrops, duração, gêneros e disponibilidade no Brasil. Sem essa chave, o sistema informa explicitamente que a disponibilidade não foi confirmada e oferece links de conferência no JustWatch, IMDb e Letterboxd; o modelo nunca é tratado como banco de dados.

## Planos, créditos e checkout

O acesso ao motor de recomendação é pago. **Um crédito equivale a uma consulta** — um filme, uma série ou uma novela — e os créditos são diários, renovados à meia-noite de Brasília.

| Plano | Preço mensal | Créditos |
| --- | --- | --- |
| Silver | R$ 15,00 | 2 consultas por dia |
| Gold | R$ 25,00 | 5 consultas por dia |
| Diamante | R$ 30,00 | Ilimitado dentro do ciclo de 30 dias |

O checkout usa a API do ASAAS. Ao escolher um plano, a plataforma cria (ou reaproveita) o cliente no ASAAS, abre uma assinatura mensal e devolve a fatura para pagamento; a fatura abre em uma nova aba para que a sessão da plataforma continue viva. **Nenhum resultado é liberado antes da confirmação do pagamento**: enquanto a cobrança estiver pendente, `POST /api/recommend` responde `402` e a interface leva o cliente de volta à tela de planos.

A confirmação chega por dois caminhos independentes, de propósito. O primeiro é o webhook `POST /api/billing/webhook`, cadastrado no painel do ASAAS; ele exige o cabeçalho `asaas-access-token` igual a `ASAAS_WEBHOOK_TOKEN` e recusa qualquer evento sem esse token — sem a variável configurada, a rota rejeita tudo. O segundo é a consulta ativa: toda leitura de `GET /api/billing/status` pergunta ao ASAAS se a cobrança foi liquidada, de modo que o acesso é liberado mesmo se o webhook não chegar. O botão “Já paguei — confirmar agora” usa exatamente esse caminho.

O crédito é debitado antes da chamada ao modelo e estornado quando ela falha, então uma indisponibilidade da OpenAI não consome a consulta do cliente. A leitura, a checagem e a gravação do saldo acontecem sob o mesmo lock, para que duas buscas simultâneas não gastem o mesmo crédito. O ciclo dura 30 dias a partir da confirmação; vencido o ciclo sem novo pagamento, a assinatura volta para pendente e o acesso fecha.

## Marcações do cliente

Cada card de indicação traz três botões: **👍 Gostei**, **👎 Não gostei** e **✅ Já assisti**. A opinião e o “já assisti” são independentes — dá para marcar só que assistiu, sem opinar — e clicar de novo na mesma opinião a desfaz. As marcações ficam gravadas na conta do usuário logado, não no navegador.

Essas marcações viajam **no prompt enviado ao GPT**: o que o cliente já assistiu é listado como proibido de recomendar de novo, o que ele gostou orienta o modelo a buscar obras de clima parecido, e o que não gostou vira instrução de evitar títulos semelhantes. Sem nenhuma marcação, o prompt continua idêntico ao do MVP — nenhuma instrução vazia é adicionada.

O botão **Marcações** no topo abre o histórico completo, com o título, o ano e as marcas de cada obra. Cada linha tem um botão **Remover** que apaga aquela marcação individualmente, devolvendo o título às indicações futuras.

## Navegação e sessão

A plataforma é uma página só, mas cada tela entra no histórico do navegador via `history.pushState`. O **botão voltar nativo do celular** percorre os sete filtros, o resumo, os resultados, os planos, o checkout e as marcações, sem recarregar a página nem sair do app.

A sessão termina quando o cliente sai da página. O cookie `pipoca_session` é um cookie de sessão de navegador — sem `Max-Age` nem `Expires` —, e ao descarregar a página o front envia um `navigator.sendBeacon` para `/api/auth/logout`, encerrando a sessão no servidor. Voltar à plataforma exige um novo login. `SESSION_TTL_SECONDS` (padrão 8 horas) é apenas o teto de segurança para abas deixadas abertas.

## Painel do administrador (`/admin`)

O painel administrativo tem **página e login próprios** em `https://SEU-DOMINIO/admin`. Ele não depende da navegação da plataforma: abra o endereço, entre com as credenciais de administrador e você já cai na lista de acessos.

Existem dois caminhos para ter uma conta de administrador:

1. **Administrador raiz por variável de ambiente.** Defina `ADMIN_EMAIL` e `ADMIN_PASSWORD` no projeto. Esse acesso funciona mesmo com a base de contas vazia e é o que destrava o painel na primeira vez. Sem essas duas variáveis em produção, **nenhum** login de administrador é aceito.
2. **Administradores gravados na base.** Já dentro do painel, use “Tornar admin” em qualquer conta liberada, ou crie uma conta com o papel *Administrador* no bloco “Criar acesso manualmente”. Esses administradores entram pelo mesmo `/admin` com o e-mail e a senha deles.

No painel você vê os contadores (pendentes, liberados, rejeitados, administradores, total), busca por e-mail, filtra por situação e, em cada conta, pode **liberar**, **rejeitar**, **voltar para pendente**, **definir uma nova senha**, **promover/rebaixar** e **excluir**. Há ainda um bloco para criar um acesso já liberado, sem passar pela fila de aprovação. Por segurança, o administrador logado não consegue rejeitar, excluir, despromover nem voltar a própria conta para pendente — outra conta de administrador precisa fazer isso.

O último bloco, **Diagnóstico do deploy**, mostra em qual armazenamento as contas estão sendo gravadas e quais variáveis o ambiente encontrou. A mesma informação, sem nenhum segredo, está disponível publicamente em `GET /api/health` — é o endereço mais rápido para descobrir por que um cadastro falhou.

## Banco de contas

O backend de contas é escolhido automaticamente a partir das variáveis presentes no ambiente, nesta ordem:

| Prioridade | Modo | Variáveis | Onde criar |
| --- | --- | --- | --- |
| 1 | `postgres` | `POSTGRES_URL` ou `DATABASE_URL` | Vercel → Storage → Create Database → Neon Postgres |
| 2 | `vercel-blob` | `BLOB_READ_WRITE_TOKEN` | Vercel → Storage → Create → Blob (acesso privado) |
| 3 | `redis-rest` | `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Vercel → Storage → Upstash for Redis |
| 4 | `arquivo-local` | `USER_STORE_FILE` | apenas desenvolvimento local |

**O filesystem das funções serverless é temporário.** Se o deploy de produção subir sem nenhuma das três primeiras opções, o cadastro responde `503` com a mensagem que explica exatamente o que fazer, em vez de fingir que gravou. Para resolver:

1. Abra o projeto na Vercel → aba **Storage** → **Create Database**.
2. Escolha **Neon Postgres** (recomendado), **Upstash for Redis** ou **Blob** e conecte ao projeto.
3. A Vercel injeta a variável correspondente automaticamente em todos os ambientes.
4. Faça um novo deploy (**Deployments → Redeploy**) para as variáveis entrarem em vigor.

No modo `postgres`, a tabela `pipoca_play_store` é criada sozinha na primeira gravação; não é preciso rodar nenhuma migração. O driver `psycopg[binary]` já está em `requirements.txt`.

### Usando Supabase em vez de Neon

O modo `postgres` funciona com qualquer Postgres, incluindo Supabase — mas **use a string de "Transaction pooler"**, nunca a de conexão direta. No painel do Supabase: **Project Settings → Database → Connection string → aba "Transaction pooler"**. Ela tem o formato:

```
postgresql://postgres.<ref-do-projeto>:[SUA-SENHA]@aws-0-<região>.pooler.supabase.com:6543/postgres
```

A conexão direta (`db.<ref-do-projeto>.supabase.co:5432`, mostrada na aba "URI") só tem endereço **IPv6**, e as funções serverless da Vercel não têm saída IPv6 — a conexão falha mesmo com a senha certa. Se isso acontecer, `/api/health` e a mensagem de erro do cadastro já apontam essa causa específica.

## Executar localmente

1. Copie `.env.example` para `.env`.
2. Mantenha `ENVIRONMENT` diferente de `production` para executar localmente. O admin de desenvolvimento usa `admin@pipocaplay.com` / `admin123`; troque esses valores antes de qualquer uso real.
3. Defina `AUTH_SECRET`, `ADMIN_EMAIL` e `ADMIN_PASSWORD` com valores privados e fortes. Não publique `.env`.
4. Em desenvolvimento, `USER_STORE_FILE=data/users.json` cria a base local automaticamente.
5. Defina `OPENAI_API_KEY` e, se disponível, `TMDB_API_KEY`.
6. Para testar o checkout, use `ASAAS_ENVIRONMENT=sandbox` com uma chave do ambiente de testes do ASAAS. Sem `ASAAS_API_KEY` a tela de planos avisa que o pagamento não está configurado e nenhuma assinatura é criada.
7. Execute `python3 server.py` dentro desta pasta.
8. Abra `http://127.0.0.1:8000` para a plataforma e `http://127.0.0.1:8000/admin` para o painel.

A chave da OpenAI é lida apenas pelo servidor. O navegador envia os sete filtros para `POST /api/recommend` apenas depois do login.

Os testes rodam com `python3 -m unittest test_app`.

## Rotas

| Rota | Método | Acesso | Uso |
| --- | --- | --- | --- |
| `/` | GET | público | Landing page e plataforma |
| `/admin` | GET | público (a página), painel só com sessão admin | Painel do administrador |
| `/api/health` | GET | público | Diagnóstico do deploy, sem segredos |
| `/api/auth/register` | POST | público | Cadastro do cliente (entra como `pending`) |
| `/api/auth/login` | POST | público | Cria a sessão por cookie |
| `/api/auth/logout` | POST | público | Encerra a sessão |
| `/api/auth/me` | GET | público | Sessão atual |
| `/api/auth/status` | GET | token do pedido | Acompanhamento do cadastro |
| `/api/admin/status` | GET | admin | Configuração, contadores e lista de contas |
| `/api/admin/users` | GET/POST | admin | Ações administrativas |
| `/api/billing/plans` | GET | público | Catálogo dos três planos |
| `/api/billing/status` | GET | usuário autenticado | Assinatura e créditos do dia; confirma o pagamento no ASAAS |
| `/api/billing/checkout` | POST | usuário autenticado | Abre a assinatura no ASAAS e devolve a fatura |
| `/api/billing/webhook` | POST | ASAAS (`asaas-access-token`) | Confirmação de pagamento |
| `/api/marks` | GET/POST | usuário autenticado | Marcações do cliente |
| `/api/recommend` | POST | usuário pagante com crédito | Motor de recomendação |

As ações aceitas em `POST /api/admin/users` são `approve`, `reject`, `pending`, `delete`, `promote`, `demote`, `set_password` e `create`.

Em `POST /api/marks`, `action: "save"` grava a marcação de uma obra (`title_original`, `title_pt`, `year`, `opinion` com `liked`/`disliked`/vazio e `watched`) e `action: "delete"` remove uma marcação pelo seu `id`. `GET /api/billing/status?sync=0` lê apenas o que já está gravado, sem consultar o ASAAS.

## Publicação na Vercel

O arquivo `vercel.json` e as funções em `api/` deixam o repositório pronto para a Vercel. Em **Project Settings → Environment Variables**, configure, por ambiente:

| Variável | Obrigatória | Uso |
| --- | --- | --- |
| `OPENAI_API_KEY` | Sim | Chave privada para gerar recomendações. |
| `OPENAI_MODEL` | Sim | Modelo disponível no projeto OpenAI. |
| `AUTH_SECRET` | Sim | Segredo longo e aleatório para assinar sessões. |
| `ADMIN_EMAIL` | Sim | E-mail do administrador que abre o painel `/admin`. |
| `ADMIN_PASSWORD` | Sim | Senha privada do administrador. |
| `POSTGRES_URL` / `DATABASE_URL` | Uma das três | Banco Postgres das contas; injetada ao conectar o Neon. |
| `BLOB_READ_WRITE_TOKEN` | Uma das três | Token da loja Vercel Blob privada. |
| `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Uma das três | Redis REST (Upstash / Vercel KV). |
| `USER_STORE_BLOB_PATH` | Não | Objeto privado do Blob; padrão `pipoca-play/users.json`. |
| `USER_STORE_FILE` | Local | Caminho do JSON local; não persiste em produção serverless. |
| `USER_STORE_MODE` | Não | Força um backend específico; use apenas para depurar. |
| `ASAAS_API_KEY` | Sim | Chave da API do ASAAS. Sem ela nenhum plano pode ser assinado. |
| `ASAAS_WEBHOOK_TOKEN` | Sim | Token do webhook de pagamento; o mesmo valor cadastrado no painel do ASAAS. |
| `ASAAS_ENVIRONMENT` | Não | `sandbox` para testes; em branco usa a API de produção. |
| `ASAAS_API_URL` | Não | Sobrescreve a URL base da API do ASAAS. |
| `ASAAS_BILLING_TYPE` | Não | Forma de pagamento da fatura; padrão `UNDEFINED` (cliente escolhe). |
| `SESSION_TTL_SECONDS` | Não | Teto da sessão em segundos; padrão 28800 (8 horas). |
| `TMDB_API_KEY` | Não | Habilita enriquecimento de catálogo e disponibilidade no Brasil. |
| `ENVIRONMENT=production` | Recomendada | Desativa credenciais padrão de desenvolvimento e ativa cookies seguros. |

O cadastro de clientes segue os estados `pending`, `approved` e `rejected`. Somente contas `approved` conseguem criar sessão, e apenas as que têm assinatura paga e crédito no dia chegam ao motor de recomendação; toda rota administrativa exige uma sessão com papel `admin`. O painel mostra, para cada conta, o plano contratado e se o pagamento está confirmado. Depois do deploy, confira `GET /api/health`: se `storage.persistent` vier `false`, o banco ainda não está conectado. Recuperação de senha pelo próprio cliente, e-mail transacional e auditoria de ações administrativas permanecem como evoluções futuras.

Não coloque chaves no HTML, no Git ou em mensagens de erro. Se uma chave tiver sido exposta anteriormente, revogue-a no respectivo provedor antes de publicar.
