# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------


import threading
import sys
import time
import re
import requests
import textwrap
import websocket
from azure.cli.core.azclierror import UnclassifiedUserFault
from azure.cli.core._profile import Profile
from azext_cloudshell._client_factory import _resource_client_factory
from urllib.parse import urlparse

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-instance-attributes
class GlobalVariables:
    def __init__(self):
        self.websocket_instance = None
        self.terminal_instance = None
        self.serial_console_instance = None
        self.terminating_app = False
        self.loading = True
        self.first_message = True
        self.block_print = False
        self.trycount = 0
        self.os_is_windows = False


class PrintClass:
    CYAN = 36
    YELLOW = 33
    RED = 91

    def __init__(self):
        self.message_buffer = ""

    def print(self, message, color=None, buffer=True):
        if color:
            message = "\x1b[" + str(color) + "m" + message + "\x1b[0m"
        if GV.block_print and buffer:
            self.message_buffer += message
        else:
            if not GV.block_print:
                self.empty_message_buffer()
            if isinstance(message, (bytes, bytearray)):
                message = message.decode("utf-8")
            print(message, end="", flush=True)

    def clear_screen(self, buffer=True):
        self.print("\x1b[2J\x1b[0;0H", buffer=buffer)

    def clear_line(self, buffer=True):
        self.print("\x1b[2K\x1b[1G", buffer=buffer)

    def cursor_up(self, buffer=True):
        self.print("\x1b[A", buffer=buffer)

    def set_cursor_horizontal_position(self, col, buffer=True):
        self.print("\x1b[" + str(col) + "G", buffer=buffer)

    def empty_message_buffer(self):
        print(self.message_buffer, end="", flush=True)
        self.message_buffer = ""

    def get_cursor_position(self, getch):
        self.print("\x1b[6n", buffer=False)
        buf = ""
        while True:
            c = getch().decode()
            buf += c
            if c == "R":
                break
        try:
            matches = re.match(r"^\x1b\[(\d*);(\d*)R", buf)
            groups = matches.groups()
        except AttributeError:
            return 1, 1
        return int(groups[0]), int(groups[1])

    def get_terminal_width(self, getch):
        self.hide_cursor(buffer=False)
        _, original_col = self.get_cursor_position(getch)
        self.set_cursor_horizontal_position(999, buffer=False)
        _, width = self.get_cursor_position(getch)
        self.set_cursor_horizontal_position(original_col, buffer=False)
        self.show_cursor(buffer=False)
        return width

    def hide_cursor(self, buffer=True):
        self.print("\x1b[?25l", buffer=buffer)

    def show_cursor(self, buffer=True):
        self.print("\x1b[?25h", buffer=buffer)

    @staticmethod
    def _get_max_width_of_string(s):
        max_width = -1
        curr_width = 0
        i = 0
        while i < len(s):
            if s[i] == '\r' or s[i] == '\n':
                i += 2
                max_width = max(curr_width, max_width)
                curr_width = 0
            else:
                i += 1
                curr_width += 1
        return max(max_width, curr_width)

    def prompt(self, getch, message):
        GV.block_print = True
        width = self.get_terminal_width(getch)
        _, col = self.get_cursor_position(getch)
        # adjust message if it is too wide to fit in console
        if width < self._get_max_width_of_string(message):
            wrapped = textwrap.wrap(message.replace(
                "\r\n", " ").replace("\n\r", " "), width=width)
            message = "\r\n".join(wrapped)
        lines = message.count("\r\n") + message.count("\n\r") + 1
        self.print("\r\n" + message, color=PrintClass.YELLOW, buffer=False)
        c = getch()
        self.hide_cursor(buffer=False)
        for _ in range(lines):
            # self.clear_line(buffer=False)
            self.cursor_up(buffer=False)
        self.set_cursor_horizontal_position(col, buffer=False)
        self.show_cursor(buffer=False)
        self.empty_message_buffer()
        GV.block_print = False
        return c


def quitapp(from_websocket=False, message="", error_message=None, error_recommendation=None, error_func=None):
    PC.print(message + "\r\n", color=PrintClass.RED)
    GV.terminating_app = True
    GV.loading = False
    if GV.terminal_instance:
        GV.terminal_instance.revert_terminal()
        GV.terminal_instance = None
    if not from_websocket and GV.websocket_instance:
        GV.websocket_instance.close()
        GV.websocket_instance = None
    if error_message and error_func:
        raise error_func(error_message, error_recommendation)
    sys.exit()


GV = GlobalVariables()
PC = PrintClass()


# pylint: disable=too-few-public-methods
class _Getch:
    def __init__(self):
        if sys.platform.startswith('win'):
            import ctypes
            from ctypes import wintypes
            STD_INPUT_HANDLE = -10
            self.h_in = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
            self.lp_buffer = ctypes.create_string_buffer(1)
            self.lp_number_of_chars_read = wintypes.DWORD()
            self.n_number_of_chars_to_read = wintypes.DWORD()
            self.n_number_of_chars_to_read.value = 1
            self.impl = self._getch_windows
        else:
            self.impl = self._getch_unix

    def __call__(self):
        return self.impl()

    @staticmethod
    def _getch_unix():
        return sys.stdin.read(1).encode()

    def _getch_windows(self):
        import ctypes
        status = ctypes.windll.kernel32.ReadConsoleW(self.h_in,
                                                     self.lp_buffer,
                                                     self.n_number_of_chars_to_read,
                                                     ctypes.byref(
                                                         self.lp_number_of_chars_read),
                                                     None)
        if status == 0:
            quitapp()
        return chr(self.lp_buffer.raw[0]).encode()


class Terminal:
    ERROR_MESSAGE = "Unable to configure terminal."
    RECOMMENDATION = ("Make sure that app in running in a terminal on a Windows 10 "
                      "or Unix based machine. Versions earlier than Windows 10 are not supported.")

    def __init__(self):
        self.win_original_out_mode = None
        self.win_original_in_mode = None
        self.win_out = None
        self.win_in = None
        self.unix_original_mode = None

    def configure_terminal(self):
        if sys.platform.startswith('win'):
            import colorama
            import ctypes
            from ctypes import wintypes
            colorama.deinit()
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
            ENABLE_ECHO_INPUT = 0x0004
            ENABLE_LINE_INPUT = 0x0002
            ENABLE_PROCESSED_INPUT = 0x0001
            STD_OUTPUT_HANDLE = -11
            STD_INPUT_HANDLE = -10
            DISABLE = ~(ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT |
                        ENABLE_PROCESSED_INPUT)

            kernel32 = ctypes.windll.kernel32
            dw_original_out_mode = wintypes.DWORD()
            dw_original_in_mode = wintypes.DWORD()
            self.win_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            self.win_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if (not kernel32.GetConsoleMode(self.win_out, ctypes.byref(dw_original_out_mode)) or
                    not kernel32.GetConsoleMode(self.win_in, ctypes.byref(dw_original_in_mode))):
                quitapp(error_message=Terminal.ERROR_MESSAGE,
                        error_recommendation=Terminal.RECOMMENDATION, error_func=UnclassifiedUserFault)

            self.win_original_out_mode = dw_original_out_mode.value
            self.win_original_in_mode = dw_original_in_mode.value

            dw_out_mode = self.win_original_out_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            dw_in_mode = (self.win_original_in_mode |
                          ENABLE_VIRTUAL_TERMINAL_INPUT) & DISABLE

            if (not kernel32.SetConsoleMode(self.win_out, dw_out_mode) or
                    not kernel32.SetConsoleMode(self.win_in, dw_in_mode)):
                quitapp(error_message=Terminal.ERROR_MESSAGE,
                        error_recommendation=Terminal.RECOMMENDATION, error_func=UnclassifiedUserFault)
        else:
            try:
                import tty
                import termios  # pylint: disable=import-error
                fd = sys.stdin.fileno()
            except (ModuleNotFoundError, ValueError):
                quitapp(error_message=Terminal.ERROR_MESSAGE,
                        error_recommendation=Terminal.RECOMMENDATION, error_func=UnclassifiedUserFault)

            self.unix_original_mode = termios.tcgetattr(fd)
            tty.setraw(fd)

    def revert_terminal(self):
        if sys.platform.startswith('win'):
            import ctypes
            kernel32 = ctypes.windll.kernel32
            if self.win_original_out_mode:
                kernel32.SetConsoleMode(self.win_out, self.win_original_out_mode)
            if self.win_original_in_mode:
                kernel32.SetConsoleMode(self.win_in, self.win_original_in_mode)
        else:
            if self.unix_original_mode:
                import termios  # pylint: disable=import-error
                try:
                    fd = sys.stdin.fileno()
                except ValueError:
                    return
                termios.tcsetattr(fd, termios.TCSADRAIN, self.unix_original_mode)


class CloudShell:
    def __init__(self, cmd):
        self.resource_client = _resource_client_factory(cmd.cli_ctx)

    @staticmethod
    def listen_for_keys():
        getch = _Getch()
        while True:
            c = getch()
            if GV.websocket_instance and not GV.first_message:
                if c == b'\x1d':
                    message = "Press q to quit cloud shell\r\n"
                    c = PC.prompt(getch, message)
                    if c == b'q':
                        PC.clear_screen()
                        PC.print('Closing cloud shell', color=PC.CYAN)
                        quitapp()
                        return
                    if c != b'\x1d':
                        continue
                try:
                    if GV.websocket_instance:
                        GV.websocket_instance.send(c)
                except (AttributeError, websocket.WebSocketConnectionClosedException):
                    quitapp()
                    sys.exit()
                    pass
            else:
                if c == b'\r' and not GV.loading:
                    GV.serial_console_instance.connect()
                elif c == b'\x1d':
                    c = PC.prompt(getch, "Press q to quit cloud shell\r\n")
                    if c == b'q':
                        quitapp()
                        return

    @staticmethod
    def connect_loading_message_linux():
        PC.clear_screen()
        PC.print("For more information on the Azure Cloud Shell, see <https://aka.ms/cloudshell>.\r\n",
                 color=PrintClass.YELLOW)
        indx = 0
        number_of_squares = 3
        chars = ["\u25A1"] * number_of_squares
        while GV.loading:
            PC.hide_cursor()
            chars_copy = chars.copy()
            chars_copy[indx] = "\u25A0"
            squares = " ".join(chars_copy)
            PC.clear_line()
            PC.print("Connecting to cloud shell " +
                     squares, color=PrintClass.CYAN)
            PC.show_cursor()
            indx = (indx + 1) % number_of_squares
            time.sleep(0.5)

    @staticmethod
    def connect_loading_message_windows():
        PC.clear_screen()
        indx = 0
        number_of_squares = 3
        chars = ["\u25A1"] * number_of_squares
        while GV.loading:
            PC.hide_cursor()
            chars_copy = chars.copy()
            chars_copy[indx] = "\u25A0"
            squares = " ".join(chars_copy)
            PC.clear_line()
            PC.print("Connecting to cloud shell " +
                     squares, color=PrintClass.CYAN)
            PC.show_cursor()
            indx = (indx + 1) % number_of_squares
            time.sleep(0.5)

    @staticmethod
    def send_loading_message(loading_text):
        indx = 0
        number_of_squares = 3
        chars = ["\u25A1"] * number_of_squares
        while GV.loading:
            chars_copy = chars.copy()
            chars_copy[indx] = "\u25A0"
            squares = " ".join(chars_copy)
            print(loading_text + "   " + squares, end="\r")
            indx = (indx + 1) % number_of_squares
            time.sleep(0.5)

    # Returns True if successful, False otherwise
    def load_websocket_url(self):
        token_info, _, _ = Profile().get_raw_token()
        access_token = token_info[1]
        try:
            terminal_create = self.create_terminal("linux")
            console_uri = terminal_create.properties["uri"]

            response = self.initialize_terminal(
                access_token,
                console_uri,
                "bash",
                {"cols": 186, "rows": 25}
            )
            terminal_init = response.json()

            if "error" in terminal_init:
                self.delete_terminal()
                return self.load_websocket_url()

            uri = terminal_init["socketUri"]
            self.websocket_url = uri

        except Exception as e:  # pylint: disable=bare-except
            PC.print(e)
            return False
        return True

    def create_terminal(self, osType: str):
        poller = self.resource_client.begin_create_or_update_by_id(
            "/providers/Microsoft.Portal/consoles/default",
            "2023-02-01-preview",
            {"properties": {"osType": osType}},
        )
        while True:
            result = poller.result(1)
            if poller.done():
                break
        return result

    def delete_terminal(self):
        poller = self.resource_client.begin_delete_by_id(
            "/providers/Microsoft.Portal/consoles/default",
            "2023-02-01-preview"
        )
        while True:
            result = poller.result(1)
            if poller.done():
                break
        return result

    def initialize_terminal(self, access_token: str, console_uri: str, shell_type: str, initial_size: dict):
        raw_url = f'{console_uri}/terminals?cols={initial_size["cols"]}&rows={initial_size["rows"]}&shell={shell_type}'
        parsed_url = urlparse(raw_url)

        scheme = parsed_url.scheme
        hostname = parsed_url.hostname
        path = parsed_url.path

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Referer": f"{scheme}://{hostname}/$hc{path}",
        }
        return requests.post(raw_url, headers=headers, json={})

    def connect(self):
        def on_open(_):
            pass

        def on_message(_, message):
            if GV.first_message:
                GV.websocket_instance.send("\n")
                GV.first_message = False
                GV.loading = False
                PC.clear_screen()
            else:
                # Detect an empty bytes frame used as sentinel before close.
                if message == b"":
                    PC.print("\r\nConnection Closed: Press \"Enter\" to reconnect or \"Ctrl+]+q\" to exit...", color=PrintClass.RED)
                    GV.websocket_instance = None
                else:
                    # Normal payload
                    PC.print(message)

        def on_error(*_):
            pass

        def on_close(_):
            GV.loading = False
            if not GV.terminating_app:
                if GV.first_message:
                    message = ("\r\nCould not establish connection to cloud shell. "
                               "Make sure that it is powered on and press \"Enter\" try again...")
                    PC.print(message, color=PrintClass.RED)
                else:
                    PC.print("\r\nConnection Closed: Press \"Enter\" to reconnect...", color=PrintClass.RED)
                GV.websocket_instance = None

        def connect_thread():
            if self.load_websocket_url():
                GV.websocket_instance = websocket.WebSocketApp(
                    self.websocket_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close)
                GV.websocket_instance.run_forever(skip_utf8_validation=True)
            else:
                GV.loading = False
                message = ("\r\nAn unexpected error occurred. Could not establish connection to cloud shell. "
                           "Check network connection and press \"Enter\" to try again...")
                PC.print(message, color=PrintClass.RED)

        GV.loading = True
        GV.first_message = True
        if GV.os_is_windows:
            th1 = threading.Thread(
                target=self.connect_loading_message_windows, args=())
        else:
            th1 = threading.Thread(
                target=self.connect_loading_message_linux, args=())
        th1.daemon = True
        th1.start()

        th2 = threading.Thread(target=connect_thread, args=())
        th2.daemon = True
        th2.start()

    def launch_console(self):
        GV.terminal_instance = Terminal()
        GV.terminal_instance.configure_terminal()
        th = threading.Thread(target=self.listen_for_keys, args=())
        th.daemon = True
        th.start()
        self.connect()
        th.join()


def connect_cloudshell(cmd):
    GV.serial_console_instance = CloudShell(cmd)
    GV.serial_console_instance.launch_console()
