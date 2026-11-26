package br.com.facincanitech.ftcamtv;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.UiModeManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.media.AudioManager;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.HttpAuthHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Repete a tela do FTCam que roda no notebook (igual abrir no navegador): o notebook continua
 * sendo a estação principal, o app só assiste ao vivo, liga o som e vê as gravações.
 *
 * Na TV box (controle remoto):
 *   setas       escolhem a câmera; ← na primeira câmera abre as gravações
 *   OK          tela cheia com som / liga e desliga o som / assiste a gravação / pausa
 *   Voltar      volta (vídeo → lista → câmeras); na grade, 2x pra sair
 *   Menu / ⚙    ajustes do app (endereço do notebook, abrir ao ligar)
 * No celular/tablet abre o layout normal de toque do FTCam (o FTCam-Celular.apk é sempre assim).
 */
public class MainActivity extends Activity {

    private final Handler ui = new Handler(Looper.getMainLooper());
    private Store store;
    private WebView web;
    private TextView aviso;
    private boolean tv;
    private int falhas = 0;
    private int senhaTentativas = 0;
    private boolean procurando = false;
    private long ultimoVoltar = 0;
    private String carregado = "";
    /** a última carga deu erro (não deixa o onPageFinished esconder o aviso) */
    private boolean erroNaCarga = false;
    /** o FTCam pediu senha e a nossa está vazia/errada: para de tentar até o usuário arrumar */
    private boolean precisaSenha = false;

    private final Runnable recarregar = this::carregar;

    /** vigia: se a página não estiver viva, recarrega */
    private final Runnable vigia = new Runnable() {
        @Override public void run() {
            if (web != null && aviso.getVisibility() != View.VISIBLE) {
                web.evaluateJavascript("(typeof S!=='undefined'&&!!S.estado)?'ok':'no'", r -> {
                    if (r == null || !r.contains("ok")) carregar();
                });
            }
            ui.postDelayed(this, 30000);
        }
    };

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);   // botões de volume mexem no som do vídeo
        store = new Store(this);
        tv = ehTv();

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);                 // lembra grade/volume como no navegador
        s.setMediaPlaybackRequiresUserGesture(false); // som e vídeo tocam sem precisar tocar na tela
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView v, String url) {
                if (!url.startsWith("about:") && !erroNaCarga && !precisaSenha) {
                    aviso.setVisibility(View.GONE);
                    falhas = 0;
                    senhaTentativas = 0;
                }
            }
            @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) falhou("Não achei o FTCam em " + store.endereco());
            }
            // Android 5.1 (Fire TV antigo) só chama esta versão
            @SuppressWarnings("deprecation")
            @Override public void onReceivedError(WebView v, int code, String desc, String url) {
                if (android.os.Build.VERSION.SDK_INT < 23) falhou("Não achei o FTCam em " + store.endereco());
            }
            @Override public void onReceivedHttpAuthRequest(WebView v, HttpAuthHandler h, String host, String realm) {
                if (!store.senha().isEmpty() && senhaTentativas < 1) {
                    senhaTentativas++;
                    h.proceed("ftcam", store.senha());
                } else {
                    h.cancel();
                    pedirSenha(store.senha().isEmpty()
                            ? "O FTCam do notebook tem senha."
                            : "A senha salva no app está errada.");
                }
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        aviso = new TextView(this);
        aviso.setTextColor(Color.WHITE);
        aviso.setTextSize(20);
        aviso.setGravity(Gravity.CENTER);
        aviso.setBackgroundColor(0xEE0B1118);
        aviso.setPadding(60, 60, 60, 60);
        // no celular: tocar no aviso abre os ajustes
        aviso.setOnClickListener(v -> startActivity(new Intent(this, SettingsActivity.class)));
        root.addView(aviso, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
    }

    /** TV box: modo TV do aparelho ou sem tela de toque. */
    private boolean ehTv() {
        if (BuildConfig.CELULAR) return false;   // APK do celular: sempre layout de toque
        UiModeManager um = (UiModeManager) getSystemService(UI_MODE_SERVICE);
        if (um != null && um.getCurrentModeType() == Configuration.UI_MODE_TYPE_TELEVISION) return true;
        return !getPackageManager().hasSystemFeature(PackageManager.FEATURE_TOUCHSCREEN);
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (tv) hideSystemBars();
        precisaSenha = false;   // voltou dos ajustes: tenta de novo com a senha nova
        if (store.endereco().isEmpty()) {
            startActivity(new Intent(this, SettingsActivity.class));
            mostrar(tv ? "Aperte Menu pra colocar o endereço do FTCam" : "Toque aqui pra colocar o endereço do FTCam");
            return;
        }
        if (!store.url(tv).equals(carregado) || erroNaCarga) carregar();
        ui.removeCallbacks(vigia);
        ui.postDelayed(vigia, 30000);
    }

    @Override
    protected void onStop() {
        super.onStop();
        ui.removeCallbacks(vigia);
        ui.removeCallbacks(recarregar);
        // para o vídeo e o som quando sai do app (economiza a TV box e a rede)
        carregado = "";
        web.loadUrl("about:blank");
    }

    private void carregar() {
        ui.removeCallbacks(recarregar);
        carregado = store.url(tv);
        if (carregado.isEmpty()) return;
        erroNaCarga = false;
        senhaTentativas = 0;
        mostrar("Conectando no FTCam…\n" + store.endereco());
        web.loadUrl(carregado);
        web.requestFocus();
    }

    /** Não conectou: tenta de novo pra sempre; depois de algumas falhas, procura o notebook na rede. */
    private void falhou(String msg) {
        if (precisaSenha) return;   // esperando a senha: não fica tentando e piscando
        erroNaCarga = true;
        falhas++;
        mostrar(msg + "\n\nTentando de novo…");
        ui.removeCallbacks(recarregar);
        ui.postDelayed(recarregar, 5000);
        if (falhas >= 3 && !procurando) {
            procurando = true;
            mostrar(msg + "\n\nProcurando o FTCam na rede…");
            new Thread(() -> {
                String achou = Finder.find(this);
                ui.post(() -> {
                    procurando = false;
                    if (achou != null && !achou.equals(store.endereco())) {
                        store.setEndereco(achou);
                        Toast.makeText(this, "Achei o FTCam em " + achou, Toast.LENGTH_LONG).show();
                        carregar();
                    }
                });
            }).start();
        }
    }

    /** Para o laço de tentativas, deixa o aviso fixo e abre os ajustes pra colocar a senha. */
    private void pedirSenha(String motivo) {
        precisaSenha = true;
        erroNaCarga = true;
        ui.removeCallbacks(recarregar);
        mostrar(motivo + "\n\n" + (tv ? "Aperte OK ou Menu pra colocar a senha do painel"
                : "Toque aqui pra colocar a senha do painel"));
        startActivity(new Intent(this, SettingsActivity.class));
    }

    private void mostrar(String msg) {
        aviso.setText(msg);
        aviso.setVisibility(View.VISIBLE);
    }

    private void sairComDoisVoltar() {
        long now = System.currentTimeMillis();
        if (now - ultimoVoltar < 2500) finish();
        else {
            ultimoVoltar = now;
            Toast.makeText(this, "Aperte Voltar de novo pra sair", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        int code = e.getKeyCode();
        if (e.getAction() == KeyEvent.ACTION_DOWN && e.getRepeatCount() == 0) {
            switch (code) {
                case KeyEvent.KEYCODE_MENU:
                case KeyEvent.KEYCODE_SETTINGS:
                case KeyEvent.KEYCODE_INFO:
                    startActivity(new Intent(this, SettingsActivity.class));
                    return true;
                case KeyEvent.KEYCODE_DPAD_CENTER:
                case KeyEvent.KEYCODE_ENTER:
                    if (aviso.getVisibility() == View.VISIBLE) {
                        startActivity(new Intent(this, SettingsActivity.class));
                        return true;
                    }
                    break;
                case KeyEvent.KEYCODE_BACK:
                    if (aviso.getVisibility() == View.VISIBLE) { sairComDoisVoltar(); return true; }
                    if (tv) {
                        // a página volta sozinha (vídeo → lista → câmeras); se já está na grade, sai
                        web.evaluateJavascript("window.tvBack?String(window.tvBack()):'false'", r -> {
                            if (r == null || !r.contains("true")) sairComDoisVoltar();
                        });
                    } else if (web.canGoBack()) {
                        web.goBack();   // no celular o FTCam já fecha telas pelo "voltar"
                    } else {
                        sairComDoisVoltar();
                    }
                    return true;
            }
        }
        if (code == KeyEvent.KEYCODE_BACK) return true;
        if (tv && (code == KeyEvent.KEYCODE_VOLUME_UP || code == KeyEvent.KEYCODE_VOLUME_DOWN
                || code == KeyEvent.KEYCODE_VOLUME_MUTE || code == KeyEvent.KEYCODE_MUTE)) {
            if (e.getAction() == KeyEvent.ACTION_DOWN) volume(code);
            return true;
        }
        // o OK do controle vira Enter pra página
        if (tv && (code == KeyEvent.KEYCODE_DPAD_CENTER || code == KeyEvent.KEYCODE_NUMPAD_ENTER)) {
            KeyEvent enter = new KeyEvent(e.getDownTime(), e.getEventTime(), e.getAction(),
                    KeyEvent.KEYCODE_ENTER, e.getRepeatCount(), e.getMetaState());
            return web.dispatchKeyEvent(enter);
        }
        return super.dispatchKeyEvent(e);
    }

    /**
     * Volume pelo controle da TV box. Sobe primeiro o volume do aparelho; quando ele já está no
     * máximo, sobe o amplificador do FTCam (até 300%). Pra baixar, faz o caminho de volta.
     * Mudo liga/desliga o som da câmera. A página mostra o aviso com o volume.
     */
    private void volume(int code) {
        AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
        int cur = am.getStreamVolume(AudioManager.STREAM_MUSIC);
        int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        if (code == KeyEvent.KEYCODE_VOLUME_MUTE || code == KeyEvent.KEYCODE_MUTE) {
            web.evaluateJavascript("(function(){try{var id=S.sound;if(!id){var t=document.querySelectorAll('#grid > .tile')[S.layout==='1'?0:(S.tvSel||0)];"
                    + "id=t&&t.id?t.id.replace('t-',''):null}if(id){toggleSound(id);toast(S.sound?'Som ligado':'Som desligado (mudo)','info')}"
                    + "else toast('Escolha uma câmera pra ouvir','info')}catch(x){}})()", null);
            return;
        }
        boolean up = code == KeyEvent.KEYCODE_VOLUME_UP;
        if (up && cur < max) {
            am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_RAISE, 0);
            avisoVolume(Math.round((cur + 1) * 100f / max));
        } else if (!up) {
            // baixa primeiro o amplificador do FTCam (se passou de 100%), depois o do aparelho
            web.evaluateJavascript("(typeof volPct==='function'&&volPct()>100)?(setVol(volPct()-20),'amp'):'sys'", r -> {
                if (r != null && r.contains("amp")) {
                    web.evaluateJavascript("toast('Volume '+volPct()+'% (amplificado)','info')", null);
                } else if (cur > 0) {
                    am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_LOWER, 0);
                    avisoVolume(Math.round((cur - 1) * 100f / max));
                } else {
                    avisoVolume(0);
                }
            });
        } else {
            // aparelho no máximo: amplifica no FTCam
            web.evaluateJavascript("typeof volPct==='function'&&(setVol(Math.min(300,volPct()+20)),toast('Volume '+volPct()+'% (amplificado)','info'))", null);
        }
    }

    private void avisoVolume(int pct) {
        web.evaluateJavascript("typeof toast==='function'&&toast('" + (pct == 0 ? "Volume 0% (mudo)" : "Volume " + pct + "%")
                + "'+(S.sound?'':' · aperte OK numa câmera pra ouvir'),'info')", null);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus && tv) hideSystemBars();
    }

    private void hideSystemBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }
}
