package br.com.facincanitech.ftcamtv;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Procura o FTCam na rede (porta 8080) quando o IP do notebook mudar. */
public class Finder {

    /** Devolve "ip:8080" do FTCam ou null. Demora alguns segundos: chamar fora da thread da tela. */
    public static String find(Context ctx) {
        String prefix = subnet(ctx);
        if (prefix == null) return null;
        AtomicReference<String> found = new AtomicReference<>(null);
        ExecutorService pool = Executors.newFixedThreadPool(32);
        for (int i = 1; i < 255; i++) {
            final String ip = prefix + i;
            pool.execute(() -> {
                if (found.get() == null && isFtcam(ip + ":8080")) found.compareAndSet(null, ip + ":8080");
            });
        }
        pool.shutdown();
        try { pool.awaitTermination(30, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
        return found.get();
    }

    public static boolean isFtcam(String hostPort) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + hostPort + "/api/estado").openConnection();
            c.setConnectTimeout(600);
            c.setReadTimeout(1500);
            int code = c.getResponseCode();
            if (code == 401) return "Basic realm=\"FTCam\"".equals(c.getHeaderField("WWW-Authenticate"));
            if (code != 200) return false;
            byte[] buf = new byte[4096];
            InputStream in = c.getInputStream();
            int n = in.read(buf);
            return n > 0 && new String(buf, 0, n, "UTF-8").contains("cameras");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** "192.168.1." da rede em que a TV box está. */
    private static String subnet(Context ctx) {
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network net = cm.getActiveNetwork();
            LinkProperties lp = cm.getLinkProperties(net);
            if (lp == null) return null;
            for (LinkAddress a : lp.getLinkAddresses()) {
                if (a.getAddress() instanceof Inet4Address) {
                    String ip = a.getAddress().getHostAddress();
                    return ip.substring(0, ip.lastIndexOf('.') + 1);
                }
            }
        } catch (Exception ignored) { }
        return null;
    }
}
