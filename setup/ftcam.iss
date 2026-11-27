; Instalador FTCam-Setup.exe (gerado pelo GitHub a cada versão).
; Ele extrai o programa numa pasta temporária e roda o instalar.bat, que faz o resto:
; Python, WebView2, bibliotecas, ffmpeg, modelos de IA, atalhos e firewall.
#define Versao GetEnv("FTCAM_VERSAO")

[Setup]
AppId={{6D486D04-F234-4516-88BA-2913C01FA1BF}
AppName=FTCam
AppVersion={#Versao}
AppPublisher=FacincaniTech
AppPublisherURL=https://facincanitech.github.io/ftcam/
VersionInfoVersion={#Versao}
VersionInfoDescription=Instalador do FTCam
OutputBaseFilename=FTCam-Setup
OutputDir=..\out
SetupIconFile=..\pc\icon.ico
WizardStyle=modern
PrivilegesRequired=admin
CreateAppDir=no
Uninstallable=no
DisableProgramGroupPage=yes
DisableReadyPage=no
Compression=lzma2
SolidCompression=yes
LicenseFile=..\LICENSE

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Messages]
pt.WelcomeLabel2=Isto vai instalar o FTCam {#Versao}, o DVR grátis pras suas câmeras IP.%n%nDepois de clicar em Instalar, abre uma janela preta que baixa o que faltar (Python, ffmpeg e a IA) e faz 3 perguntas rápidas. Precisa de internet.
pt.ReadyLabel1=Pronto pra instalar o FTCam.
pt.FinishedHeadingLabel=FTCam instalado
pt.FinishedLabelNoIcons=O FTCam foi instalado. Abra pelo atalho FTCam.%n%nAchou um bug ou quer sugerir algo novo? Fale comigo:%nWhatsApp: (17) 99631-6439%nEmail: facincanitech@gmail.com
pt.FinishedLabel=O FTCam foi instalado. Abra pelo atalho FTCam.%n%nAchou um bug ou quer sugerir algo novo? Fale comigo:%nWhatsApp: (17) 99631-6439%nEmail: facincanitech@gmail.com

[Files]
Source: "..\pc\*"; DestDir: "{tmp}\FTCam"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "__pycache__\*,dados\*,gravacoes\*,modelos\*,ffmpeg\*"
Source: "..\LICENSE"; DestDir: "{tmp}\FTCam"; DestName: "LICENCA.txt"

[Run]
Filename: "{cmd}"; Parameters: "/c ""{tmp}\FTCam\instalar.bat"""; WorkingDir: "{tmp}\FTCam"; StatusMsg: "Instalando o FTCam (siga a janela preta)..."; Flags: waituntilterminated
