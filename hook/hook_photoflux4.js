// hook_photoflux.js — 解出 libphotoflux_security.so 的全部密钥
// hook 点:
//  1. NativeSecrets.computeHmacSignature / getApiSecret — 直接调用捕获返回值
//  2. HmacRequestSigner.sign — 捕获 HMAC 输入/输出
//  3. OkHttp 请求 — 抓所有带 X-Signature/X-API-Secret/Authorization 的真实请求

Java.perform(function () {
    console.log("[*] PhotoFlux hook loaded");

    // ---- 0. pairip license check bypass ----
    try {
        var Instr = Java.use("android.app.Instrumentation");
        Instr.checkStartActivityResult.overload('int', 'java.lang.Object').implementation = function (res, msg) {
            // 完全替换: 负结果一律静默放行（pairip LicenseActivity 启动失败不崩）
            console.log("[PAIRIP] checkStartActivityResult res=" + res + " msg=" + msg + " (suppressed)");
        };
    } catch (e) { console.log("[!] pairip bypass fail: " + e); }
    try {
        var LC = Java.use("com.pairip.licensecheck.LicenseClient");
        var methods = LC.class.getDeclaredMethods();
        methods.forEach(function (m) {
            var name = m.getName();
            try {
                LC[name].overloads.forEach(function (ov) {
                    var rt = m.getReturnType().getName();
                    ov.implementation = function () {
                        console.log("[PAIRIP] " + name + " suppressed");
                        if (rt === "void") return;
                        if (rt === "boolean") return true;
                        return null;
                    };
                });
            } catch (e) {}
        });
        console.log("[*] LicenseClient all methods suppressed (" + methods.length + ")");
    } catch (e) { console.log("[!] pairip LicenseClient: " + e); }

    // ---- 1. NativeSecrets 全量枚举 ----
    try {
        var NativeSecrets = Java.use("com.technosub.unlimitedaiimagen.data.remote.security.NativeSecrets");
        var inst = NativeSecrets.INSTANCE.value;

        // getApiSecret(0..8) 主动遍历
        for (var i = 0; i <= 8; i++) {
            try {
                var secret = inst.getApiSecret(i);
                console.log("[KEY] getApiSecret(" + i + ") = " + secret);
            } catch (e) {
                console.log("[KEY] getApiSecret(" + i + ") ERR: " + e);
            }
        }

        // computeHmacSignature hook — 记录输入输出
        NativeSecrets.computeHmacSignature.overload('int', 'java.lang.String', 'java.lang.String', 'java.lang.String')
            .implementation = function (svc, ts, nonce, bodyHash) {
                var out = this.computeHmacSignature(svc, ts, nonce, bodyHash);
                console.log("[HMAC] svc=" + svc + " ts=" + ts + " nonce=" + nonce + " bodyHash=" + bodyHash);
                console.log("[HMAC] -> sig=" + out);
                return out;
            };
        console.log("[*] NativeSecrets hooked");
    } catch (e) {
        console.log("[!] NativeSecrets hook fail: " + e);
    }

    // ---- 2. HmacRequestSigner.sign 捕获完整签名链 ----
    try {
        var Signer = Java.use("com.technosub.unlimitedaiimagen.data.remote.security.HmacRequestSigner");
        Signer.sign.overload('int', '[B').implementation = function (svc, body) {
            var res = this.sign(svc, body);
            console.log("[SIGN] svc=" + svc + " bodyLen=" + body.length);
            console.log("[SIGN] -> sig=" + res.getSignature() + " ts=" + res.getTimestamp() + " nonce=" + res.getNonce());
            return res;
        };
        console.log("[*] HmacRequestSigner hooked");
    } catch (e) {
        console.log("[!] Signer hook fail: " + e);
    }

    // ---- 3. OkHttp Request 拦截 — 抓真实请求（URL + 头 + body）----
    try {
        var Request = Java.use("okhttp3.Request");
        var Buffer = Java.use("okio.Buffer");
        Request.newBuilder.overload().implementation = function () {
            var b = this.newBuilder();
            try {
                var url = this.url().toString();
                var hdrs = this.headers().toString();
                if (url.indexOf("unlim") !== -1 || url.indexOf("fireworks") !== -1 ||
                    url.indexOf("decohere") !== -1 || url.indexOf("imagenai") !== -1 ||
                    hdrs.indexOf("X-Signature") !== -1 || hdrs.indexOf("X-API-Secret") !== -1) {
                    console.log("\n[REQ] " + this.method() + " " + url);
                    console.log("[REQ-HDRS] " + hdrs);
                    var body = this.body();
                    if (body !== null) {
                        var buf = Buffer.$new();
                        body.writeTo(buf);
                        console.log("[REQ-BODY] " + buf.readUtf8());
                    }
                }
            } catch (e) {}
            return b;
        };
        console.log("[*] OkHttp hooked");
    } catch (e) {
        console.log("[!] OkHttp hook fail: " + e);
    }
});

// ---- 4. 主动灌登录态: 拿 REST idToken 直接在 app 内 signInWithCredential ----
setTimeout(function () {
    Java.perform(function () {
        try {
            var json = Java.use("java.lang.String");
            var FirebaseUser = Java.use("com.google.firebase.auth.FirebaseUser");
            // 用 GetCredential 方式复杂；改为直接 hook FirebaseAuth 里 currentUser getter 拦截点不行。
            // 最稳: 调 FirebaseAuth.getInstance().signInWithCustomToken? 需要后端签发。
            // 改用 Email 凭据 signIn:
            var FirebaseAuth = Java.use("com.google.firebase.auth.FirebaseAuth");
            var EmailCred = Java.use("com.google.firebase.auth.EmailAuthProvider");
            var fAuth = FirebaseAuth.getInstance();
            var email = "pf81_noxhhbuw@example.com", pwd = "PflomPXIzRhFzsN1IW!";
            fAuth.signInWithEmailAndPassword(email, pwd);
            console.log("[AUTH] signInWithEmailAndPassword fired");
        } catch (e) { console.log("[AUTH] err: " + e); }
    });
}, 8000);
