package br.com.facincanitech.ftcamtv;

import android.content.Context;
import android.content.SharedPreferences;

/** Ajustes do app, guardados na própria TV box. */
public class Store {

    private final SharedPreferences p;

    public Store(Context ctx) {
        p = ctx.getSharedPreferences("ftcamtv", Context.MODE_PRIVATE);
    }

    /** Endereço do FTCam no notebook, ex.: 192.168.1.7:8080 */
    public String endereco() { return p.getString("endereco", ""); }
    public void setEndereco(String v) { p.edit().putString("endereco", v.trim()).apply(); }

    /** Senha do painel (só se tiver colocado senha no FTCam). */
    public String senha() { return p.getString("senha", ""); }
    public void setSenha(String v) { p.edit().putString("senha", v).apply(); }

    public boolean abrirAoLigar() { return p.getBoolean("boot", true); }
    public void setAbrirAoLigar(boolean v) { p.edit().putBoolean("boot", v).apply(); }

    /** tv = layout pro controle remoto; senão, o layout normal (celular/tablet, toque) */
    public String url(boolean tv) {
        String e = endereco();
        if (e.isEmpty()) return "";
        if (!e.startsWith("http")) e = "http://" + e;
        if (!e.matches("^https?://[^/]+:\\d+.*")) e = e.replaceAll("/+$", "") + ":8080";
        return e.replaceAll("/+$", "") + (tv ? "/?tv=1" : "/?ver=1");
    }
}
