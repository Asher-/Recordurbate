class IpcConfig:
    host = "127.0.0.1"
    # Fixed, and below macOS's ephemeral range (49152-65535) so no outgoing
    # connection is ever given it; only one daemon can bind it at a time
    port = 47391
