package br.com.facincanitech.ftcamtv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Ligou a TV box: abre as câmeras sozinho (se estiver marcado nos ajustes). */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (!new Store(ctx).abrirAoLigar()) return;
        try {
            Intent i = new Intent(ctx, MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
        } catch (Exception ignored) {
            // Android 10+ pode bloquear se a permissão "sobrepor a outros apps" não foi dada
        }
    }
}
