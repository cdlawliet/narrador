# Narrador sincronizado

Interface web local que transforma legendas SRT em uma narração brasileira
sincronizada e exporta o resultado como `narration_synchronized.mp3`.

## Uso rápido no Windows

1. Confirme que o Python e o FFmpeg estão instalados e disponíveis no `PATH`.
2. Dê dois cliques em `iniciar.bat`.
3. No navegador, escolha ou arraste um arquivo `.srt`, selecione a voz e clique
   em **Gerar narração**.
4. Ouça a prévia e baixe o MP3.

Na primeira execução, o inicializador cria o ambiente virtual e instala as
dependências automaticamente. A síntese de voz requer acesso à internet.

## Execução manual

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Acesse <http://127.0.0.1:5000>. Para iniciar sem abrir o navegador
automaticamente:

```powershell
$env:NARRADOR_OPEN_BROWSER = "0"
.\.venv\Scripts\python.exe app.py
```

## Deploy no Coolify

O projeto inclui `Dockerfile` e `docker-compose.yaml` prontos para produção.

1. No Coolify, crie um recurso a partir do repositório público
   `https://github.com/cdlawliet/narrador`.
2. Selecione o Build Pack **Docker Compose**.
3. Use a branch `main`, Base Directory `/` e Docker Compose Location
   `/docker-compose.yaml`.
4. Para um domínio próprio, configure o domínio do serviço `narrador` como
   `https://narrador.seudominio.com:8000`. O sufixo `:8000` indica a porta
   interna; o acesso público continua usando HTTPS normal.
5. Faça o deploy sem ativar **Raw Compose Deployment**.

O Compose não publica portas diretamente no servidor. O tráfego passa pelo
proxy do Coolify, e `SERVICE_FQDN_NARRADOR_8000` permite gerar um domínio
automaticamente quando o servidor possui wildcard configurado. O endpoint
`/health` é usado para verificar a saúde do contêiner.

Não é necessário configurar volume persistente: uploads, segmentos de voz e o
MP3 final são temporários e removidos após a resposta ao navegador.

Mantenha **uma única réplica**: o bloqueio de geração simultânea é local ao
processo. Para arquivos próximos do limite de 3 horas, disponibilize pelo menos
6 GB de RAM ao serviço e faça um teste com arquivos representativos antes de
abrir o domínio publicamente. O processamento atual monta a linha do tempo em
memória e arquivos extensos podem consumir recursos consideráveis.

Como a aplicação não possui autenticação, proteja o domínio com uma camada de
acesso externo caso ele não deva ficar disponível ao público.

## Limites da interface

- Arquivo SRT de até 5 MB.
- Linha do tempo de até 3 horas.
- Até 10.000 blocos e 300.000 caracteres falados.
- Uma geração por vez no servidor local.

O arquivo é processado localmente, mas o texto das falas é enviado ao serviço
online do Microsoft Edge TTS. Se ele falhar, o Google TTS é utilizado como
alternativa.

## Linha de comando

O gerador original continua disponível:

```powershell
.\.venv\Scripts\python.exe narrador.py input.srt
```
