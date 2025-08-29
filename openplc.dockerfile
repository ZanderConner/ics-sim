FROM ubuntu:24.04

ENV Debian_FRONTEND=noninteractive

RUN apt update
RUN apt install -y python3 sudo make systemctl git

RUN git clone https://github.com/thiagoralves/OpenPLC_v3.git 
RUN cd OpenPLC_v3 && ./install.sh linux

EXPOSE 8080

WORKDIR /OpenPLC_v3
ENTRYPOINT ["./start_openplc.sh"]