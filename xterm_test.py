import asyncio
import threading
import json
import webview

HOST = "192.168.0.94"
PORT = 4902


HTML = r"""
<!doctype html>
<html>
<head>

<meta charset="utf-8">

<link rel="stylesheet"
href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css"/>

<script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.js"></script>

<style>

html, body {
    margin:0;
    width:100%;
    height:100%;
    background:#000;
    overflow:hidden;
}

#terminal {
    width:100%;
    height:100%;
    padding:8px;
    box-sizing:border-box;
}

.xterm {
    height:100%;
}

</style>

</head>

<body>

<div id="terminal"></div>

<script>

const term = new Terminal({

    cursorBlink: true,

    rows: 40,
    cols: 120,

    scrollback: 5000,

    convertEol: true,

    fontSize: 15,

    fontFamily: 'Menlo, Monaco, monospace',

    theme: {
        background: '#000000',
        foreground: '#FFFFFF'
    }
});

term.open(document.getElementById('terminal'));

term.focus();

//
// affichage données venant du device
//

window.receive_from_python = function(data) {

    term.write(data);
};

//
// clavier utilisateur
//

term.onData(function(data) {

    //
    // ECHO LOCAL
    // car le WSTK ne semble pas renvoyer
    // les caractères tapés
    //

    term.write(data);

    //
    // envoyer au device
    //

    window.pywebview.api.send(data);
});

//
// message démarrage
//

term.writeln('\x1b[32mConnected to WSTK\x1b[0m');
term.writeln('');

</script>

</body>
</html>
"""


class Api:

    def __init__(self):

        self.loop = asyncio.new_event_loop()

        self.writer = None
        self.reader = None

        self.prompt = "WSTK>"

        threading.Thread(
            target=self.run_loop,
            daemon=True
        ).start()

    def run_loop(self):

        asyncio.set_event_loop(self.loop)

        self.loop.create_task(self.tcp_client())

        self.loop.run_forever()

    async def wait_for_prompt(self):

        buffer = ""

        while True:

            data = await self.reader.read(4096)

            if not data:
                break

            text = data.decode(errors="ignore")

            #
            # afficher dans xterm
            #

            js = f"receive_from_python({json.dumps(text)})"

            try:
                webview.windows[0].evaluate_js(js)
            except Exception:
                pass

            buffer += text

            #
            # attendre vrai prompt
            #

            if self.prompt in buffer:
                return

    async def send_python_command(self, cmd):

        #
        # afficher proprement la commande
        #

        js = f"receive_from_python({json.dumps(cmd + "\r\n")})"

        try:
            webview.windows[0].evaluate_js(js)
        except Exception:
            pass

        #
        # envoyer
        #

        self.writer.write((cmd + "\r\n").encode())

        await self.writer.drain()

        #
        # attendre fin commande
        #

        await self.wait_for_prompt()

    async def tcp_client(self):

        print(f"Connexion {HOST}:{PORT}")

        reader, writer = await asyncio.open_connection(
            HOST,
            PORT
        )

        self.reader = reader
        self.writer = writer

        print("TCP connecté")

        #
        # attendre prompt initial
        #

        await self.wait_for_prompt()

        #
        # automatisation SEQUENTIELLE
        #

        await self.send_python_command("help")

        await self.send_python_command("target help")

        #
        # ensuite terminal interactif
        #

        while True:

            data = await reader.read(4096)

            if not data:
                break

            text = data.decode(errors="ignore")

            js = f"receive_from_python({json.dumps(text)})"

            try:
                webview.windows[0].evaluate_js(js)
            except Exception:
                pass

    async def _send(self, data):

        if self.writer:

            self.writer.write(data.encode())

            await self.writer.drain()

    def send(self, data):

        asyncio.run_coroutine_threadsafe(
            self._send(data),
            self.loop
        )

        return "ok"


api = Api()

window = webview.create_window(
    "WSTK Terminal",
    html=HTML,
    js_api=api,
    width=1200,
    height=800
)

#
# debug=False IMPORTANT
#

webview.start(debug=False)