package br.com.facincanitech.ftcamtv;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/** Ajustes pelo controle remoto: endereço do notebook, senha e abrir ao ligar. */
public class SettingsActivity extends Activity {

    private Store store;
    private EditText endereco, senha;
    private TextView status;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        store = new Store(this);

        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(0xFF0B1118);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(48), dp(28), dp(48), dp(28));
        scroll.addView(col);
        setContentView(scroll);

        col.addView(text("FTCam TV — ajustes", 26, true));
        col.addView(text("Mostra na TV as câmeras do FTCam que roda no notebook, igual o navegador.", 14, false));

        col.addView(text("Endereço do FTCam no notebook", 18, true));
        endereco = field(col, store.endereco().isEmpty() ? "192.168.1.7:8080" : store.endereco(),
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        col.addView(text("É o mesmo endereço que você abre no navegador do celular (ex.: 192.168.1.7:8080).", 13, false));
        col.addView(button("Procurar o FTCam na rede sozinho", v -> procurar()));
        status = text("", 14, false);
        col.addView(status);

        col.addView(text("Senha do painel (só se colocou senha no FTCam)", 18, true));
        senha = field(col, store.senha(), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);

        col.addView(text("Ao ligar a TV box", 18, true));
        CheckBox boot = new CheckBox(this);
        boot.setText("Abrir as câmeras sozinho quando a TV box ligar");
        boot.setTextColor(Color.WHITE);
        boot.setTextSize(16);
        boot.setChecked(store.abrirAoLigar());
        boot.setOnCheckedChangeListener((v, on) -> store.setAbrirAoLigar(on));
        col.addView(boot);
        col.addView(text("Algumas TV box com Android 10+ só deixam abrir sozinho com a permissão abaixo.", 13, false));
        col.addView(button("Dar permissão pra abrir sozinho", v -> pedirSobreposicao()));

        Button salvar = button("✓ Salvar e ver as câmeras", v -> salvar());
        col.addView(salvar);
        salvar.requestFocus();
    }

    private void salvar() {
        String e = endereco.getText().toString().trim();
        if (e.isEmpty()) {
            Toast.makeText(this, "Coloque o endereço do FTCam", Toast.LENGTH_LONG).show();
            return;
        }
        store.setEndereco(e);
        store.setSenha(senha.getText().toString());
        finish();
    }

    private void procurar() {
        status.setText("Procurando na rede… (uns 10 segundos)");
        new Thread(() -> {
            String achou = Finder.find(this);
            runOnUiThread(() -> {
                if (achou != null) {
                    endereco.setText(achou);
                    status.setText("Achei o FTCam em " + achou + ". Agora é só salvar.");
                } else {
                    status.setText("Não achei. Confira se o notebook está ligado com o FTCam aberto e na mesma rede.");
                }
            });
        }).start();
    }

    private void pedirSobreposicao() {
        try {
            startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            Toast.makeText(this, "Esta TV box não tem essa tela (normalmente já abre sozinho).", Toast.LENGTH_LONG).show();
        }
    }

    private EditText field(LinearLayout parent, String value, int type) {
        EditText e = new EditText(this);
        e.setSingleLine(true);
        e.setInputType(type);
        e.setText(value);
        e.setTextColor(Color.WHITE);
        e.setTextSize(18);
        parent.addView(e);
        return e;
    }

    private TextView text(String s, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(bold ? Color.WHITE : 0xFF9CA3AF);
        if (bold) t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
        t.setPadding(0, dp(bold ? 18 : 4), 0, dp(6));
        return t;
    }

    private Button button(String s, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setTextSize(16);
        b.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        b.setBackgroundResource(R.drawable.btn_bg);
        b.setPadding(dp(18), dp(12), dp(18), dp(12));
        b.setFocusable(true);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2);
        lp.setMargins(0, dp(6), dp(10), dp(6));
        b.setLayoutParams(lp);
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
