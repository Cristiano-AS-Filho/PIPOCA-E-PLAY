# Auditoria — Pipoca & Play

## Diagnóstico inicial

O repositório original continha uma interface de questionário e uma função de recomendação OpenAI, mas não possuía landing page, tela de login, sessão, papéis de usuário, painel administrativo nem proteção da rota `/api/recommend`. O histórico dependia exclusivamente de `window.storage`, que não existe em navegadores comuns. A interface já tinha os sete filtros do MVP e uma experiência de resultados com três cartões.

## Correções aplicadas

A aplicação agora possui landing page pública, tela de login e cadastro individual, sessão por cookie HttpOnly assinado com HMAC, papel `admin`, painel `/api/admin/status`, logout e proteção de recomendações no backend. Cada conta de cliente possui hash PBKDF2, status `pending`, `approved` ou `rejected`, e o frontend acompanha a situação do pedido em `/api/auth/status`.

O painel administrativo passou a listar pedidos e contas, com ações protegidas para aceitar, rejeitar ou excluir usuários. A base usa JSON local em desenvolvimento e Redis REST compatível com Vercel KV/Upstash em produção serverless; sem armazenamento persistente configurado em produção, o cadastro é bloqueado com resposta controlada.

O backend agora valida o JSON da IA e exige exatamente três recomendações ordenadas. Foi criado `ContentMetadataProvider`, com adaptador TMDB opcional para poster, backdrop, duração, gêneros e disponibilidade brasileira. Sem fonte externa configurada, a interface exibe disponibilidade não confirmada em vez de atribuir fatos ao modelo.

O histórico ganhou fallback para `localStorage`, mantendo compatibilidade com o armazenamento do ambiente quando disponível. A documentação e `.env.example` foram atualizados com as variáveis de autenticação, catálogo e persistência.

## Validação visual local

A landing page foi aberta em `http://127.0.0.1:8000/` e exibiu corretamente CTA, proposta de valor e os três indicadores. O botão de entrada abriu a tela de login com abas de entrada e cadastro; a aba de cadastro exibiu e-mail, senha e confirmação de senha.

Foi criado um cadastro de teste com e-mail e senha individuais. A interface exibiu a mensagem de aguardo — “O seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.” — e o status ficou disponível para atualização automática.

O login local administrativo de teste `admin@test.local` / `admin-password` foi aceito e criou sessão. O painel exibiu uma pendência real com as ações “Aceitar acesso”, “Rejeitar” e “Excluir usuário”. Após aceitar o cadastro, os contadores mudaram para 0 pendências e 1 acesso liberado, e a conta do cliente entrou na lista como aprovada. O login do cliente com a senha cadastrada abriu corretamente o primeiro filtro da plataforma.

A partir do painel, o botão “Nova busca” continua abrindo o primeiro filtro e a seleção “Suspense / Thriller” avança automaticamente para o segundo filtro, mantendo os sete passos oficiais.

O fluxo autenticado também avançou corretamente pela vibe “Sombrio / Assustador” e pelo tempo “Padrão (90 a 120 min)”, confirmando a progressão visual e a separação entre clima e duração.

O wizard avançou corretamente por “Anos 2010s” e “Netflix”, confirmando também o filtro de plataforma independente dos demais.

As últimas etapas “Em Casal” e “Aclamados pela Crítica / Premiações (Oscar, Cannes)” levaram ao resumo “Sua busca”, que exibiu corretamente todos os sete valores antes da execução.

Após reiniciar com um `.env` de teste, uma sessão antiga foi invalidada e o navegador voltou à landing page. A tela de login aceitou o formulário de `cliente@pipocaplay.com`; o teste HTTP correspondente confirmou papel `user` e bloqueio de `/api/admin/status` com HTTP 403.

No navegador, o usuário comum foi redirecionado ao primeiro filtro sem botão de painel admin. O botão “Histórico” abriu o estado vazio corretamente, confirmando que a ausência de `window.storage` não quebra a página graças ao fallback para `localStorage`.

Foi simulada uma resposta JSON válida no navegador para não depender de chave OpenAI durante o teste. `runSearch` terminou em `results`, renderizou exatamente três recomendações, match, ratings, Oscar, links de verificação, fallback visual e aviso de disponibilidade não confirmada; também salvou um item no histórico via `localStorage`.

## Correções de persistência e acesso administrativo

O cadastro em produção falhava com “Não foi possível gravar a base de contas. Configure um armazenamento persistente.” porque o deploy não tinha nenhum armazenamento conectado e o código caía no arquivo local, que o filesystem somente leitura das funções serverless recusa.

A camada de armazenamento passou a detectar quatro backends, nesta ordem: `postgres` (`POSTGRES_URL`/`DATABASE_URL`), `vercel-blob` (`BLOB_READ_WRITE_TOKEN`), `redis-rest` (`KV_REST_API_URL`) e `arquivo-local`. O Postgres é a opção recomendada, cria a tabela `pipoca_play_store` sozinha na primeira gravação e usa `psycopg[binary]`, já declarado em `requirements.txt`. A gravação local agora recusa explicitamente o modo serverless e a mensagem de erro passou a nomear o passo a passo real na Vercel, em vez de pedir genericamente “um armazenamento persistente”. Foi adicionado `GET /api/health`, público e sem segredos, que informa qual backend está ativo, se ele é persistente e se as credenciais de administrador existem.

O acesso administrativo ganhou página dedicada em `/admin`, com login próprio, contadores, busca, filtro por situação e as ações de liberar, rejeitar, voltar para pendente, redefinir senha, promover, rebaixar, excluir e criar acesso já liberado. Além do administrador raiz definido por `ADMIN_EMAIL`/`ADMIN_PASSWORD`, contas gravadas na base podem receber o papel `admin` e entrar pelo mesmo painel. O administrador logado é impedido, no backend e na interface, de retirar o próprio acesso. O painel exibe um bloco de diagnóstico com o backend em uso e as variáveis encontradas, e mostra o passo a passo da Vercel quando nenhum banco está conectado.

As rotas de autenticação e administração passaram a compartilhar `api_core.py`, chamado tanto por `server.py` quanto pelas funções em `api/`, eliminando a duplicação que fazia o servidor local e o runtime serverless divergirem. A suíte `test_app.py` subiu de 11 para 27 testes, cobrindo o backend Postgres com driver falso, a recusa de gravação em modo serverless, o ciclo completo de ações administrativas, a proteção da própria conta, o payload de `/api/health` e as respostas de login.

## Assinaturas pagas, créditos diários, marcações e navegação (esta sessão)

O repositório não tinha nenhuma integração com o ASAAS, nenhum controle de crédito/assinatura e nenhuma marcação de "gostei"/"já assisti" — apesar da premissa inicial do pedido, isso foi confirmado por busca no código antes de implementar. Foram adicionados `plans.py` (catálogo Silver R$15/2 créditos-dia, Gold R$25/5 créditos-dia, Diamante R$30/ilimitado) e `asaas.py` (cliente HTTP do ASAAS: cliente, assinatura mensal, link de pagamento hospedado e verificação do token do webhook).

`user_store.py` ganhou os campos `plan`, `subscription` e `credits` em cada conta, com `try_consume_credit`/`refund_credit` (débito atômico sob lock, com estorno se a chamada à IA falhar depois de já ter debitado) e reset diário à meia-noite de Brasília (UTC-3 fixo, já que o Brasil não tem mais horário de verão) sem acúmulo de créditos não usados. Também ganhou `marks`, um dicionário por conta com as marcações de gostei/não gostei/já assisti, indexado por um slug determinístico do título + ano.

A lógica de recomendação (schema, prompt, chamada à OpenAI) foi movida de `server.py` para `api_core.py`, que agora expõe `recommend(session, body)`: exige sessão de usuário, debita 1 crédito, injeta o histórico de marcações no prompt (para não repetir títulos marcados e calibrar o gosto por eles) e chama a OpenAI, estornando o crédito se a chamada falhar. Sem assinatura ativa ou sem crédito disponível, a rota responde `402 Payment Required` em vez de gerar a recomendação — só o pagamento confirmado libera os resultados. Novas rotas: `GET /api/payments/plans`, `GET /api/payments/status`, `POST /api/payments/checkout` (cria cliente + assinatura no ASAAS e devolve o link de pagamento hospedado) e `POST /api/payments/webhook` (público, validado pelo header `asaas-access-token`, ativa ou revoga o acesso conforme o evento do ASAAS). `GET/POST /api/marks` lista, cria/atualiza e remove marcações; remover uma marcação faz o título voltar a poder ser recomendado.

No frontend, o login agora consulta `/api/payments/status` e leva o cliente para a tela de planos quando não há assinatura ativa; cada card de recomendação ganhou os botões "Gostei", "Não gostei" e "Já assisti", e uma nova tela "Minhas marcações" lista e permite remover cada marcação individualmente. A navegação passou a usar `history.pushState`/`popstate`, então o botão voltar nativo do navegador (inclusive no celular) percorre as telas do app em vez de sair dele. Ao sair da página (fechar a aba, navegar para fora ou fechar o app), um `sendBeacon` no evento `pagehide` encerra a sessão no servidor, então o próximo acesso sempre pede um novo login.

A suíte de testes subiu para 45 casos, cobrindo consumo/estorno de crédito, planos ilimitados, o fluxo completo de checkout com o ASAAS mockado, a ativação/desativação da assinatura pelo webhook (com e sem token correto) e o ciclo de vida das marcações (criar, listar, alternar "gostei" de volta a neutro e remover). O fluxo completo também foi validado num navegador real (Chromium via Playwright) contra o servidor local: cadastro, aprovação pelo admin, tela de planos com os preços corretos, bloqueio do checkout sem `ASAAS_API_KEY`, liberação do wizard após ativar uma assinatura, geração de resultados com resposta da IA simulada, marcação e remoção de "gostei"/"já assisti", botão voltar nativo entre as telas e o logout automático ao fechar a aba.
