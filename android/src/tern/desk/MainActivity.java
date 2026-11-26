package tern.desk;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.hardware.biometrics.BiometricManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.InputFilter;
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
    private static final String ADDRESSES = "tern.desk.ADDRESSES", PAIR = "tern.desk.PAIR";
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
    private Keys keys;
    private volatile boolean deskPage;                      // an approved desk page is up: the only time the bridge answers
    private boolean bridged, signing, fingerWait;           // the bridge is on this web view; a sign-in is under way; back from settings
    private int pairing;                                    // an older pairing's answers are dropped
    private final Act.Count count = new Act.Count() {       // the signed count, kept before it is used
        @Override public long kept() { return prefs.getLong("n", 1); }
        @Override public boolean keep(long next) { return prefs.edit().putLong("n", next).commit(); }
    };
    private final Runnable look = () -> { if (resumed && !editing) check(); };

    // ---------------------------------------------------------------- starting and stopping

    @SuppressWarnings("deprecation")
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("tern", MODE_PRIVATE);
        keys = new Keys(this);
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
        if (ADDRESSES.equals(getIntent().getAction()) || bases().isEmpty()) form(null);
        else if (PAIR.equals(getIntent().getAction())) pairIntro(null);
        else { waiting(); connect(); }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (ADDRESSES.equals(intent.getAction())) form(null);
        else if (PAIR.equals(intent.getAction()) && !bases().isEmpty()) pairIntro(null);
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        web.onResume();
        main.removeCallbacks(look);
        main.postDelayed(look, 300);
        if (fingerWait) pairFinger();                       // back from adding a fingerprint
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
        keys.close();
        root.removeAllViews();
        web.destroy();
        super.onDestroy();
    }

    @SuppressWarnings("deprecation")
    @Override public void onBackPressed() { back(); }       // before Android 13

    private void back() {
        if (editing && !bases().isEmpty()) { hideKeys(); editing = false; fingerWait = false; pairing++; if (base != null && !failed) show(null); else waiting(); connect(); return; }
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
        if (busy || signing) return;
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
        if (same) { show(null); return; }
        String at = route();
        base = to; failed = false; fresh = true;
        for (String b : bases()) share(b, to, false);
        if (device() != null) { signIn(to, at); return; }
        if (!prefs.getBoolean("offered", false)) { prefs.edit().putBoolean("offered", true).apply(); failed = true; pairIntro(null); return; }
        web.loadUrl(to + "/" + at);
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
        bridged = false; deskPage = false;
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
            if (base == null || !Link.at(url, base)) deskPage = false;
            if (!url.equals("about:blank") && (base == null || !Link.at(url, base))) v.stopLoading();
        }

        @Override public void onPageFinished(WebView v, String url) {
            if (base == null || !Link.at(url, base)) return;
            SslCertificate c = v.getCertificate();
            if (c != null && !approved(c)) { failed = true; deskPage = false; v.loadUrl("about:blank"); if (!editing) connect(); return; }
            deskPage = c != null;
            String path = Uri.parse(url).getPath();
            boolean signedIn = "/login".equals(last) && "/".equals(path);
            if (signedIn) for (String b : bases()) share(base, b, true);
            if (fresh || signedIn) { v.clearHistory(); fresh = false; }                     // back doesn't go to the sign-in
            last = path == null ? "" : path;
            if (deskPage && !bridged && device() != null) {   // a page sees it from its next load on
                bridged = true;
                v.addJavascriptInterface(new Bridge(bridge), Bridge.NAME);
                v.reload();
                return;
            }
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

    private EditText field(String k, String hint) { return input(prefs.getString(k, ""), hint, InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI); }

    private EditText input(String value, String hint, int type) {
        EditText e = new EditText(this);
        e.setText(value); e.setHint(hint); e.setTextColor(TEXT); e.setHintTextColor(GREY); e.setTypeface(Typeface.MONOSPACE); e.setTextSize(16); e.setSingleLine(true);
        e.setInputType(type); e.setPadding(dp(12), dp(12), dp(12), dp(12)); e.setBackground(fill(FILL));
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

    // ---------------------------------------------------------------- signing in, and the page's commands

    private String device() { return prefs.getString("device", null); }

    private static double now() { return System.currentTimeMillis() / 1000.0; }

    /** The app's own sign-in: signed by the everyday key, sent over the pinned connection, its cookie put in the web view's
        jar; then the pages. The password page is for a phone that isn't paired. */
    private void signIn(String to, String at) {
        String dev = device(), pin = pin();
        int t = turn;
        signing = true;
        show(column(text("tern", HEAD, 22), text("Signing in…", GREY, 16)));
        keys.sign(Keys.EVERYDAY, "sign in to tern", "your fingerprint signs this phone in to the desk",
            () -> Act.payload(dev, Act.next(count), now(), Act.SESSION), new Keys.Done() {
                @Override public void signed(String text, byte[] sig) {
                    String body = Act.body(text, Act.b64(sig), false);
                    worker.execute(() -> {
                        Link.Reply r;
                        try {
                            r = Link.post(to, "/app/session", body, pin, 8000);
                        } catch (IOException e) {
                            String why = "no answer from tern: " + e.getMessage();
                            main.post(() -> { signing = false; if (t == turn && !editing) notSignedIn(to, at, why); });
                            return;
                        }
                        main.post(() -> signedIn(t, to, at, r));
                    });
                }
                @Override public void refused(String why) { signing = false; if (t == turn && !editing) notSignedIn(to, at, why); }
                @Override public void gone() { signing = false; unpair(Keys.GONE); }
            });
    }

    private void signedIn(int t, String to, String at, Link.Reply r) {
        signing = false;
        if (t != turn || editing) return;
        String cookie = r.code == 200 ? Act.appCookie(r.cookies) : null;
        if (cookie == null) { notSignedIn(to, at, Act.error(r)); return; }
        CookieManager jar = CookieManager.getInstance();
        String c = cookie + "; Max-Age=" + SIGN_IN + "; Path=/; Secure; HttpOnly; SameSite=Strict";
        for (String b : bases()) if (!b.equals(to)) jar.setCookie(b, c);
        jar.setCookie(to, c, ok -> { if (t == turn && !editing) { web.loadUrl(to + "/" + at); show(null); } });
    }

    private void notSignedIn(String to, String at, String why) {
        failed = true; editing = true;
        show(column(text("Not signed in", WARN, 20), text(why, TEXT, 15),
            button("Try again", true, () -> { editing = false; waiting(); connect(); }),
            button("Pair this phone again", false, () -> pairIntro(null)),
            button("View only", false, () -> { editing = false; base = to; failed = false; fresh = true; web.loadUrl(to + "/" + at); show(null); }),
            button("Addresses", false, () -> form(null))));
    }

    /** This phone is no longer paired: its keys, device and app cookie go. */
    private void forget() {
        prefs.edit().remove("device").apply();
        CookieManager jar = CookieManager.getInstance();
        for (String b : bases()) jar.setCookie(b, Act.COOKIE + "=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict");
        failed = true;
    }

    private void unpair(String note) {
        Keys.delete();
        forget();
        pairIntro(note);
    }

    /** TernApp, for the page: it answers only while an approved desk page is up. */
    private final Bridge.To bridge = new Bridge.To() {
        @Override public boolean paired() { return deskPage && device() != null; }
        @Override public void sign(String id, String cmdJson, boolean live) { main.post(() -> signFor(id, cmdJson, live)); }
    };

    /** A command from the page, signed with the key it says (live: a fingerprint every time), back to the page to send. */
    private void signFor(String id, String cmdJson, boolean live) {
        if (id == null || !deskPage) return;
        String dev = device();
        if (dev == null) { page("ternRefused", id, "not sent: this phone isn't paired"); return; }
        if (cmdJson == null || !Act.command(cmdJson)) { page("ternRefused", id, "not sent: that isn't a command"); return; }
        keys.sign(live ? Keys.LIVE : Keys.EVERYDAY, live ? "live: your fingerprint" : "send to tern",
            live ? "this is for live trading, with real money" : "your fingerprint signs this for the desk",
            () -> Act.payload(dev, Act.next(count), now(), cmdJson), new Keys.Done() {
                @Override public void signed(String text, byte[] sig) { page("ternSigned", id, Act.body(text, Act.b64(sig), live)); }
                @Override public void refused(String why) { page("ternRefused", id, why); }
                @Override public void gone() { page("ternRefused", id, Keys.GONE); unpair(Keys.GONE); }
            });
    }

    /** An answer to the page, as JavaScript string literals: only to a desk page. */
    private void page(String fn, String id, String text) {
        if (deskPage) web.evaluateJavascript("window." + fn + "&&" + fn + "(" + Act.quote(id) + "," + Act.quote(text) + ")", null);
    }

    // ---------------------------------------------------------------- pairing: new keys, a fingerprint, the desk's code, a yes on the desk

    private void pairIntro(String note) {
        editing = true; turn++; busy = false; signing = false; fingerWait = false; pairing++;
        hideKeys();
        show(column(note == null ? null : text(note, WARN, 16), text("Pair this phone", HEAD, 20),
            text("Paired, this phone can send the desk commands, not only look. It makes two keys that never leave its secure hardware: "
                + "an everyday one, and one for live that asks for your fingerprint every time.", TEXT, 15),
            text("On the desk: settings (9), APP, pair a phone. It shows a 6-digit code for 5 minutes.", GREY, 14),
            button("Next", true, this::pairFinger), button(device() == null ? "Not now" : "Keep the pairing it has", false, this::leave)));
    }

    private void leave() { hideKeys(); editing = false; fingerWait = false; pairing++; waiting(); connect(); }

    private void pairFinger() {
        fingerWait = false;
        int r = Keys.ready(this);
        if (r == BiometricManager.BIOMETRIC_SUCCESS) { pairCode(null); return; }
        if (r == BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED) {
            fingerWait = true;                              // looked at again when the app is back
            show(column(text("Add a fingerprint", HEAD, 20), text("The keys need a fingerprint on this phone. Add one in the phone's settings, then come back here.", TEXT, 15),
                button("Open settings", true, this::enroll), button("Not now", false, this::leave)));
        } else if (r == BiometricManager.BIOMETRIC_ERROR_HW_UNAVAILABLE) {
            show(column(text("The fingerprint reader isn't ready", WARN, 20), text("Try again in a moment.", TEXT, 15),
                button("Try again", true, this::pairFinger), button("Not now", false, this::leave)));
        } else {
            show(column(text("This phone can't be paired", WARN, 20), text("It has no fingerprint reader strong enough for the keys. You can still look at the pages.", TEXT, 15),
                button("Back", true, this::leave)));
        }
    }

    private void enroll() {
        Intent i = Build.VERSION.SDK_INT >= 30
            ? new Intent(Settings.ACTION_BIOMETRIC_ENROLL).putExtra(Settings.EXTRA_BIOMETRIC_AUTHENTICATORS_ALLOWED, BiometricManager.Authenticators.BIOMETRIC_STRONG)
            : new Intent(Settings.ACTION_SECURITY_SETTINGS);
        try {
            startActivity(i);
        } catch (ActivityNotFoundException e) {
            try { startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS)); } catch (ActivityNotFoundException x) { /* only by hand, then */ }
        }
    }

    private void pairCode(String wrong) {
        EditText code = input("", "123456", InputType.TYPE_CLASS_NUMBER), name = input(prefs.getString("name", Build.MODEL), "phone", InputType.TYPE_CLASS_TEXT);
        code.setFilters(new InputFilter[] { new InputFilter.LengthFilter(6) });
        name.setFilters(new InputFilter[] { new InputFilter.LengthFilter(24) });
        TextView bad = text(wrong == null ? "" : wrong, WARN, 14);
        show(column(text("Pair this phone", HEAD, 20), text("The code the desk shows", GREY, 14), code, text("This phone's name, for the desk's list", GREY, 14), name, bad,
            button("Pair", true, () -> {
                String c = code.getText().toString().trim(), typed = name.getText().toString().trim(), n = typed.isEmpty() ? Build.MODEL : typed;
                if (!c.matches("[0-9]{6}")) { bad.setText("the code is the 6 digits the desk shows"); return; }
                prefs.edit().putString("name", n).apply();
                hideKeys();
                pairStart(c, n);
            }),
            button("Not now", false, this::leave)));
    }

    /** New keys in place of any before: from here this phone is not paired until the desk says yes. */
    private void pairStart(String code, String name) {
        int p = ++pairing;
        List<String> all = bases();
        String pin = pin();
        if (all.isEmpty()) { form(null); return; }
        if (pin == null) { pairCode("the desk's certificate isn't approved yet: open its pages once first"); return; }
        forget();
        show(column(text("Pairing…", HEAD, 20), text("Making this phone's keys.", GREY, 15)));
        worker.execute(() -> {
            String e, l;
            try {
                Keys.make(this);
                e = Keys.publicB64(Keys.EVERYDAY);
                l = Keys.publicB64(Keys.LIVE);
            } catch (GeneralSecurityException | IOException | RuntimeException x) {
                String why = x.getMessage() == null ? x.getClass().getSimpleName() : x.getMessage();
                main.post(() -> { if (p == pairing) pairCode("the keys weren't made: " + why); });
                return;
            }
            main.post(() -> { if (p == pairing) pairSign(p, all, pin, code, name, e, l); });
        });
    }

    private void pairSign(int p, List<String> all, String pin, String code, String name, String e, String l) {
        keys.sign(Keys.EVERYDAY, "pair this phone with tern", "your fingerprint signs this phone's new keys", () -> Act.pairText(code, name, e, l), new Keys.Done() {
            @Override public void signed(String text, byte[] sig) { if (p == pairing) pairAsk(p, all, pin, Act.pairBody(code, name, e, l, Act.b64(sig)), Act.words(e, l)); }
            @Override public void refused(String why) { if (p == pairing) pairCode(why.replaceFirst("^not sent", "not paired")); }
            @Override public void gone() { if (p == pairing) pairCode("the keys didn't take: try again"); }
        });
    }

    /** The keys, signed, to the desk at whichever address has the approved certificate. */
    private void pairAsk(int p, List<String> all, String pin, String body, String words) {
        show(column(text("Pairing…", HEAD, 20), text("Asking the desk.", GREY, 15)));
        worker.execute(() -> {
            List<Link.Answer> got = Link.find(all, pin);
            Link.Answer a = got.get(0);
            String why, offer = null;
            if (got.size() != 1 || !pin.equals(a.pin)) why = "no answer from tern with the approved certificate";
            else {
                try {
                    Link.Reply r = Link.post(a.base, "/app/pair", body, pin, 8000);
                    offer = r.code == 200 ? Act.field(r.body, "offer") : null;
                    why = offer == null ? Act.error(r) : words.equals(Act.field(r.body, "words")) ? null : "the desk got other keys than this phone's: not paired";
                } catch (IOException x) {
                    why = "no answer from tern: " + x.getMessage();
                }
            }
            String said = why, id = offer;
            main.post(() -> {
                if (p != pairing) return;
                if (said != null) pairCode(said); else pairWait(p, a.base, id, words);
            });
        });
    }

    private void pairWait(int p, String at, String offer, String words) {
        show(column(text("Press y on the desk", HEAD, 20), text("The desk shows this phone's words. If they are these, press y there:", TEXT, 15), text(words, WARN, 26),
            text("If they aren't, press n there.", GREY, 14), button("Cancel", false, () -> pairIntro(null))));
        poll(p, at, offer, SystemClock.elapsedRealtime() + Act.PAIR_WAIT);
    }

    /** Every 2 s, for up to 5 minutes: has the desk said yes? */
    private void poll(int p, String at, String offer, long until) {
        String pin = pin();
        main.postDelayed(() -> {
            if (p != pairing) return;
            worker.execute(() -> {
                String state, more;
                try {
                    Link.Reply r = Link.post(at, "/app/paired", Act.offerBody(offer), pin, 8000);
                    state = r.code == 200 ? String.valueOf(Act.field(r.body, "state")) : "refused";
                    more = r.code == 200 ? Act.field(r.body, "device") : Act.error(r);
                } catch (IOException e) {
                    state = "waiting"; more = null;                 // no answer this time
                }
                String s = state, m = more;
                main.post(() -> polled(p, at, offer, until, s, m));
            });
        }, Act.POLL);
    }

    private void polled(int p, String at, String offer, long until, String state, String more) {
        if (p != pairing) return;
        if ("paired".equals(state) && more != null) {
            prefs.edit().putString("device", more).apply();
            pairing++; editing = false; failed = true;
            waiting(); connect();                               // signs in, then the pages
        } else if ("declined".equals(state)) pairIntro("not paired: the desk said no");
        else if ("refused".equals(state)) pairIntro("not paired: " + more);
        else if ("unknown".equals(state)) pairIntro("that pairing is over: make a new code on the desk");
        else if ("expired".equals(state) || SystemClock.elapsedRealtime() >= until) pairIntro("the pairing ran out: make a new code on the desk");
        else poll(p, at, offer, until);
    }

    private void hideKeys() { getSystemService(InputMethodManager.class).hideSoftInputFromWindow(root.getWindowToken(), 0); }
}
