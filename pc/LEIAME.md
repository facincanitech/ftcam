# FTCam — DVR para câmeras IP

Programa de Windows que grava **só os alertas** das suas câmeras (Tapo e qualquer câmera RTSP/ONVIF),
com visualização ao vivo, linha do tempo e limpeza automática.

## Instalar em qualquer PC (recomendado)

1. Descompacte o zip em qualquer lugar (ex.: Downloads).
2. Dê dois cliques em **`instalar.bat`** e aceite o pedido de administrador.
3. Ele confere e instala sozinho o que faltar: **Python 3.12**, **WebView2**, as dependências,
   o **ffmpeg** e os **modelos de IA**. Também libera a porta 8080 no Firewall da rede de casa.
4. Pergunta se o PC deve **ficar sem suspender na tomada** (diga Sim num DVR 24h) e se o FTCam
   deve **abrir com o Windows**.
5. Abra pelo atalho **FTCam** na área de trabalho.

Instala em `%LOCALAPPDATA%\Programs\FTCam` (pasta do usuário, o Windows sempre deixa gravar).
Pra atualizar, rode o `instalar.bat` da versão nova: configurações, rostos e gravações são mantidos.
Pra remover: `desinstalar.bat` (pergunta se apaga as gravações).

> Não instale em `C:\Program Files`: é protegida e o Windows bloqueia a gravação.

## Gerar o .exe (uma vez)

1. Tenha o **Python 3.11 ou 3.12** instalado (python.org, marque *Add to PATH*).
2. Dê dois cliques em **`build.bat`**.
   - Ele instala as dependências, empacota o programa e baixa o ffmpeg sozinho.
3. No fim abre a pasta `dist\FTCam` — o programa é o **`FTCam.exe`**.
   Pode mover essa pasta inteira pra onde quiser (ex.: o notebook velho).

> Quer só testar sem gerar o .exe? Rode `rodar_sem_exe.bat` (precisa do ffmpeg no PATH ou `ffmpeg.exe` nesta pasta).

## Primeiro uso

1. Na Tapo: app → câmera → **Configurações → Avançado → Conta da câmera** → crie usuário e senha.
2. Abra o FTCam → **Câmeras → Procurar na rede**. Ele acha as câmeras sozinho (ONVIF + varredura da rede).
3. Clique em **Adicionar**, coloque o usuário/senha da Conta da câmera, **Testar conexão** e **Salvar**.

## O que ele faz

- **Ao vivo** em grade (1×1, 2×2, 3×3, 4×4 ou automático). Clique numa câmera pra focar nela.
- **Grava só alertas**: 30 s a 2 min, começando 10 s *antes* do movimento. Tudo ajustável.
- **Duas detecções à escolha, por câmera:**
  - *Da própria Tapo* (ONVIF): usa a detecção de movimento/pessoa da câmera, com as zonas e
    a sensibilidade do app Tapo. Gasta quase zero de CPU. Se falhar, o FTCam assume sozinho.
  - *Do FTCam*: análise de imagem própria, funciona com qualquer câmera, com controle de sensibilidade.
- **Alertas** na lateral com miniatura, tipo (movimento / pessoa / manual), filtro por data, câmera e tipo.
- **Linha do tempo** (24h, 3, 7 ou 30 dias) com os alertas de cada câmera.
- **Player** com voltar/avançar 10 s, velocidade 0,5× a 4×, alerta anterior/próximo (setas do teclado), baixar e apagar.
- **Gravar agora** (alerta manual) e **foto** da câmera.
- **Controles da Tapo** (experimental): sirene, modo privacidade, LED e visão noturna.
  Precisa da *senha da conta Tapo* (a do app) no cadastro da câmera.
- **Apaga sozinho** depois de X dias, e apaga os mais antigos se o HD ficar abaixo do espaço mínimo.
- **Webhook**: a cada alerta manda um POST com foto em base64 (ex.: Edge Function do Thoth).
- **Bandeja do Windows**: fechar a janela só esconde, continua gravando. Sair de vez pelo ícone da bandeja.
- **Iniciar com o Windows** (Ajustes → Sistema): abre minimizado e já grava.
- **Acesso pelo celular** na mesma rede: `http://IP-DO-PC:8080` (senha opcional em Ajustes → Acesso).

## Onde ficam as coisas

```
FTCam\
  FTCam.exe
  ffmpeg.exe
  dados\config.json      ← configurações e câmeras
  dados\ftcam.log        ← log (útil se algo der errado)
  gravacoes\<câmera>\<dia>\HH-MM-SS.mp4 / .jpg / .json
```

## Rodar no Linux (note sem tela)

```bash
sudo apt install ffmpeg python3-venv
python3 -m venv .venv && .venv/bin/pip install opencv-python-headless flask psutil pytapo
.venv/bin/python app.py --sem-janela
```
E acesse `http://IP-DO-NOTE:8080` de outro aparelho.

## Problemas comuns

- **Câmera "Off"**: confira IP, usuário e senha da *Conta da câmera* (não é a conta do app).
- **"Detecção da Tapo indisponível"**: a câmera não respondeu o ONVIF na porta 2020 — o FTCam
  usa a própria detecção automaticamente. Pode trocar pra "Do FTCam" no cadastro.
- **Muito alarme falso** (no modo FTCam): baixe a sensibilidade.
- **Modo privacidade ligado**: a câmera para de mandar imagem e nada é gravado.
