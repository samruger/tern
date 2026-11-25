package tern.desk;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.window.OnBackInvokedDispatcher;

import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** The desk's web pages, full screen: from the first of its addresses that answers with the approved certificate. */
public final class MainActivity extends Activity {
    private static final String ADDRESSES = "tern.desk.ADDRESSES";
    private static final int TEXT = 0xffc0caf5, GREY = 0xff737aa2, HEAD = 0xff7aa2f7, FILL = 0xff24283b, WARN = 0xffe0af68;
    private static final int SIGN_IN = 30 * 86400;          // how long a sign-in lasts (webauth.TTL)
    private static final Pattern COOKIE = Pattern.compile("(?:^|;\\s*)((?:__Host-tern_s|tern_ui)=[^;]*)");

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private SharedPreferences prefs;
    private FrameLayout root;
    private View screen;                                    // what is over the web page, or null
    private WebView web;
    private String base, route = "";                        // the address in use; the page to open at
    private boolean failed, fresh, busy, resumed, editing;  // editing: a screen the person has to leave by themselves
    private int turn;                                       // an older look for the desk than this one is dropped
    private ConnectivityManager.NetworkCallback network;
    private final Runnable look = () -> { if (resumed && !editing) check(); };

    // ---------------------------------------------------------------- starting and stopping

    @SuppressWarnings("deprecation")
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("tern", MODE_PRIVATE);
        preset();
        if (state != null) route = state.getString("route", "");
        root = new FrameLayout(this);
        if (Build.VERSION.SDK_INT >= 30) {                  // the bars' and the keyboard's space is left here, so that the page needn't
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((v, in) -> {
                Insets i = in.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
                return WindowInsets.CONSUMED;
            });
        }
        setContentView(root);
        color(prefs.getInt("bg", getColor(R.color.bg)));
        makeWeb();
        if (Build.VERSION.SDK_INT >= 33) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        network = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network n) { main.removeCallbacks(look); main.postDelayed(look, 800); }
        };
        getSystemService(ConnectivityManager.class).registerDefaultNetworkCallback(network);
        if (ADDRESSES.equals(getIntent().getAction()) || bases().isEmpty()) form(null); else { waiting(); connect(); }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (ADDRESSES.equals(intent.getAction())) form(null);
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        web.onResume();
        main.removeCallbacks(look);
        main.postDelayed(look, 300);
    }

    @Override protected void onPause() {
        resumed = false;
        web.onPause();
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        out.putString("route", route());
    }

    @Override protected void onDestroy() {
        turn++;
        main.removeCallbacksAndMessages(null);
        getSystemService(ConnectivityManager.class).unregisterNetworkCallback(network);
        worker.shutdownNow();
        root.removeAllViews();
        web.destroy();
        super.onDestroy();
    }

    @SuppressWarnings("deprecation")
    @Override public void onBackPressed() { back(); }       // before Android 13

    private void back() {
        if (editing && !bases().isEmpty()) { hideKeys(); editing = false; if (base != null && !failed) show(null); else waiting(); connect(); return; }
        if (screen == null && web.canGoBack()) { web.goBack(); return; }
        moveTaskToBack(true);
    }

    // ---------------------------------------------------------------- what is kept: the addresses and the certificate approved for them

    /** What the apk was built with, once: a newer apk's takes the place of what was typed. */
    private void preset() {
        Properties p = new Properties();
        try (InputStream in = getAssets().open("preset.properties")) {
            p.load(new InputStreamReader(in, StandardCharsets.UTF_8));
        } catch (IOException e) {
            return;                                         // built without any
        }
        String home = p.getProperty("home", ""), outside = p.getProperty("outside", ""), pin = p.getProperty("pin", ""), all = home + "\n" + outside + "\n" + pin;
        if (all.equals(prefs.getString("preset", ""))) return;
        SharedPreferences.Editor e = prefs.edit().putString("preset", all).putString("home", home).putString("outside", outside);
        if (pin.isEmpty()) e.remove("pin"); else e.putString("pin", pin);
        e.apply();
    }

    private List<String> bases() {
        List<String> out = new ArrayList<>();
        for (String k : new String[] { "home", "outside" }) {
            try {
                String b = Link.base(prefs.getString(k, ""));
                if (b != null && !out.contains(b)) out.add(b);
            } catch (IllegalArgumentException e) { /* the form says so */ }
        }
        return out;
    }

    private String pin() { return prefs.getString("pin", null); }

    // ---------------------------------------------------------------- finding the desk

    /** Whether the address in use still answers; another is looked for if not. */
    private void check() {
        if (busy) return;
        if (base == null || failed || screen != null) { connect(); return; }
        String was = base, pin = pin();
        int t = turn;
        worker.execute(() -> {
            Link.Answer a = Link.ask(was, 4000);
            main.post(() -> { if (t == turn && !editing && (a.pin == null || !a.pin.equals(pin))) connect(); });
        });
    }

    private void connect() {
        List<String> all = bases();
        if (all.isEmpty()) { form(null); return; }
        int t = ++turn;
        String pin = pin();
        busy = true;
        worker.execute(() -> {
            List<Link.Answer> got = Link.find(all, pin);
            main.post(() -> { if (t == turn) { busy = false; found(got, pin); } });
        });
    }

    private void found(List<Link.Answer> got, String pin) {
        if (editing) return;
        Link.Answer a = got.get(0);
        if (got.size() == 1 && a.pin != null && (pin == null || pin.equals(a.pin))) {
            if (pin == null) approve(a, null); else open(a.base);
            return;
        }
        for (Link.Answer x : got) if (x.pin != null && pin != null) { changed(x, pin); return; }
        StringBuilder why = new StringBuilder();
        for (Link.Answer x : got) why.append(why.length() > 0 ? "\n\n" : "").append(x.base.substring(8)).append("\n").append(x.error);
        problem("No answer from tern", why + "\n\nTrying again.", null);
        main.removeCallbacks(look);
        main.postDelayed(look, 5000);
    }

    private void open(String to) {
        boolean same = to.equals(base) && !failed && web.getUrl() != null && Link.at(web.getUrl(), to);
        if (!same) {
            String at = route();
            base = to; failed = false; fresh = true;
            for (String b : bases()) share(b, to, false);
            web.loadUrl(to + "/" + at);
        }
        show(null);
    }

    /** The page the viewer is on, to open the same one at another address. */
    private String route() {
        String u = web.getUrl();
        int i = u == null ? -1 : u.indexOf("#/");
        return i >= 0 ? u.substring(i) : u == null || u.equals("about:blank") ? route : "";
    }

    /** A sign-in at one address is good at the others: they are the same desk, with the same certificate. One made just
        now takes the place of theirs; an address opened anew only gets what it hasn't. */
    private void share(String from, String to, boolean anew) {
        CookieManager jar = CookieManager.getInstance();
        String there = jar.getCookie(from), here = jar.getCookie(to);
        if (from.equals(to) || there == null) return;
        for (Matcher m = COOKIE.matcher(there); m.find(); ) {
            String name = m.group(1).substring(0, m.group(1).indexOf('=') + 1);
            if (anew || here == null || !here.contains(name)) jar.setCookie(to, m.group(1) + "; Max-Age=" + SIGN_IN + "; Path=/; Secure; HttpOnly; SameSite=Strict");
        }
    }

    private void signedOut() {
        for (String b : bases()) CookieManager.getInstance().setCookie(b, "__Host-tern_s=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict");
    }

    // ---------------------------------------------------------------- the web page

    private void makeWeb() {
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setSupportMultipleWindows(false);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setBackgroundColor(prefs.getInt("bg", getColor(R.color.bg)));
        web.clearSslPreferences();                          // the certificate is asked about anew each time
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new Pages());
        root.addView(web, 0, new FrameLayout.LayoutParams(-1, -1));
    }

    private boolean approved(SslCertificate c) {
        X509Certificate x = c == null ? null : c.getX509Certificate();
        try {
            return x != null && pin() != null && pin().equals(Link.pin(x.getEncoded()));
        } catch (GeneralSecurityException e) {
            return false;
        }
    }

    private final class Pages extends WebViewClient {
        private String last = "";

        /** Every certificate comes here (network.xml trusts none). Only the approved one, at the desk's address, is taken. */
        @Override public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) {
            if (base != null && Link.at(e.getUrl(), base) && approved(e.getCertificate())) { h.proceed(); return; }
            h.cancel();
            failed = true;
            if (!editing) connect();
        }

        @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            Uri u = r.getUrl();
            if (base != null && Link.at(u.toString(), base)) {
                if ("/logout".equals(u.getPath())) signedOut();
                return false;
            }
            if (r.isForMainFrame() && r.hasGesture() && "https".equals(u.getScheme())) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (ActivityNotFoundException e) { /* no browser */ }
            }
            return true;
        }

        @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) {
            if (!url.equals("about:blank") && (base == null || !Link.at(url, base))) v.stopLoading();
        }

        @Override public void onPageFinished(WebView v, String url) {
            if (base == null || !Link.at(url, base)) return;
            SslCertificate c = v.getCertificate();
            if (c != null && !approved(c)) { failed = true; v.loadUrl("about:blank"); if (!editing) connect(); return; }
            String path = Uri.parse(url).getPath();
            boolean signedIn = "/login".equals(last) && "/".equals(path);
            if (signedIn) for (String b : bases()) share(base, b, true);
            if (fresh || signedIn) { v.clearHistory(); fresh = false; }                     // back doesn't go to the sign-in
            last = path == null ? "" : path;
            paint(); main.postDelayed(MainActivity.this::paint, 1500);
        }

        @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            if (!r.isForMainFrame() || editing) return;
            failed = true;
            problem("No answer from tern", r.getUrl().getAuthority() + "\n" + e.getDescription() + "\n\nTrying again.", null);
            main.removeCallbacks(look);
            main.postDelayed(look, 5000);
        }

        @Override public boolean onRenderProcessGone(WebView v, RenderProcessGoneDetail d) {
            root.removeView(v);
            v.destroy();
            makeWeb();
            failed = true;
            if (!editing) { waiting(); connect(); }
            return true;
        }
    }

    /** The space around the page in the page's own color. */
    private void paint() {
        web.evaluateJavascript("(document.querySelector('meta[name=theme-color]')||{}).content||''", s -> {
            try {
                if (s != null && s.matches("\"#[0-9a-fA-F]{6}\"")) color(Color.parseColor(s.substring(1, 8)));
            } catch (IllegalArgumentException e) { /* not one */ }
        });
    }

    @SuppressWarnings("deprecation")
    private void color(int c) {
        root.setBackgroundColor(c);
        getWindow().getDecorView().setBackgroundColor(c);
        getWindow().setStatusBarColor(c);
        getWindow().setNavigationBarColor(c);
        if (Build.VERSION.SDK_INT >= 30) {
            int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
            WindowInsetsController bars = getWindow().getInsetsController();
            if (bars != null) bars.setSystemBarsAppearance(Color.luminance(c) > 0.4 ? light : 0, light);
        }
        prefs.edit().putInt("bg", c).apply();
    }

    // ---------------------------------------------------------------- the app's own screens

    private void show(View v) {
        if (screen != null) root.removeView(screen);
        screen = v;
        web.setVisibility(v == null ? View.VISIBLE : View.INVISIBLE);
        if (v != null) root.addView(v, new FrameLayout.LayoutParams(-1, -1));
    }

    private int dp(float x) { return Math.round(x * getResources().getDisplayMetrics().density); }

    private TextView text(String s, int color, float size) {
        TextView t = new TextView(this);
        t.setText(s); t.setTextColor(color); t.setTextSize(size); t.setTypeface(Typeface.MONOSPACE); t.setPadding(0, 0, 0, dp(12));
        return t;
    }

    private GradientDrawable fill(int color) { GradientDrawable d = new GradientDrawable(); d.setColor(color); d.setCornerRadius(dp(6)); return d; }

    private TextView button(String s, boolean first, Runnable does) {
        TextView b = text(s, first ? getColor(R.color.bg) : TEXT, 16);
        b.setGravity(Gravity.CENTER); b.setMinHeight(dp(50)); b.setPadding(dp(12), dp(8), dp(12), dp(8)); b.setBackground(fill(first ? HEAD : FILL));
        b.setTypeface(Typeface.MONOSPACE, first ? Typeface.BOLD : Typeface.NORMAL);
        b.setClickable(true); b.setFocusable(true); b.setOnClickListener(v -> does.run());
        b.setAccessibilityDelegate(new View.AccessibilityDelegate() {
            @Override public void onInitializeAccessibilityNodeInfo(View v, android.view.accessibility.AccessibilityNodeInfo i) { super.onInitializeAccessibilityNodeInfo(v, i); i.setClassName("android.widget.Button"); }
        });
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
        p.topMargin = dp(12);
        b.setLayoutParams(p);
        return b;
    }

    private View column(View... kids) {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL); c.setGravity(Gravity.CENTER_VERTICAL); c.setPadding(dp(20), dp(20), dp(20), dp(20));
        for (View k : kids) if (k != null) c.addView(k, k.getLayoutParams() == null ? new LinearLayout.LayoutParams(-1, -2) : k.getLayoutParams());
        ScrollView s = new ScrollView(this);
        s.setFillViewport(true); s.setBackgroundColor(getColor(R.color.bg)); s.addView(c, new ViewGroup.LayoutParams(-1, -1));
        return s;
    }

    private void waiting() { show(column(text("tern", HEAD, 22), text("Looking for your desk…", GREY, 16))); }

    private void problem(String title, String body, View more) {
        show(column(text(title, WARN, 20), text(body, TEXT, 15), button("Try again", true, () -> { waiting(); connect(); }), more, button("Addresses", false, () -> form(null))));
    }

    private void changed(Link.Answer a, String pin) {
        problem("A different certificate", a.base.substring(8) + " answered with a certificate that isn't the one you approved, so it wasn't opened.\n\n"
            + "If you made tern a new certificate, approve it. If you didn't, that may not be your desk.", button("See the new one", false, () -> approve(a, pin)));
    }

    private void approve(Link.Answer a, String old) {
        editing = true;
        show(column(text("Is this your desk?", HEAD, 20), text(a.base.substring(8) + " has the certificate with this SHA-256 fingerprint:", TEXT, 15), text(a.pin, WARN, 15),
            old == null ? null : text("The one approved before:", GREY, 14), old == null ? null : text(old, GREY, 14),
            text("Compare it with the one on the computer:\n\nopenssl x509 -in ~/.config/tern/web-cert.pem -noout -fingerprint -sha256", GREY, 14),
            button("It's the same: approve it", true, () -> { prefs.edit().putString("pin", a.pin).apply(); editing = false; failed = true; web.clearSslPreferences(); waiting(); connect(); }),
            button("Not now", false, () -> form(null))));
    }

    private EditText field(String k, String hint) {
        EditText e = new EditText(this);
        e.setText(prefs.getString(k, "")); e.setHint(hint); e.setTextColor(TEXT); e.setHintTextColor(GREY); e.setTypeface(Typeface.MONOSPACE); e.setTextSize(16); e.setSingleLine(true);
        e.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI); e.setPadding(dp(12), dp(12), dp(12), dp(12)); e.setBackground(fill(FILL));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
        p.bottomMargin = dp(14);
        e.setLayoutParams(p);
        return e;
    }

    private void form(String wrong) {
        editing = true;
        turn++; busy = false;
        EditText home = field("home", "192.168.1.20:8787"), outside = field("outside", "desk.example.com:8787");
        TextView bad = text(wrong == null ? "" : wrong, WARN, 14);
        show(column(text("Where is your desk?", HEAD, 20), text("At home", GREY, 14), home, text("From outside, if it can be reached from there", GREY, 14), outside, bad, button("Save", true, () -> {
            String h = home.getText().toString().trim(), o = outside.getText().toString().trim();
            try {
                String a = Link.base(h), b = Link.base(o);
                if (a == null && b == null) { bad.setText("An address is needed. At home it is the one the settings box shows, under WEB."); return; }
            } catch (IllegalArgumentException e) {
                bad.setText(e.getMessage());
                return;
            }
            prefs.edit().putString("home", h).putString("outside", o).apply();
            hideKeys();
            editing = false;
            waiting(); connect();
        })));
    }

    private void hideKeys() { getSystemService(InputMethodManager.class).hideSoftInputFromWindow(root.getWindowToken(), 0); }
}
