FROM ubuntu:24.04

ENV Debian_FRONTEND=noninteractive

RUN apt update
RUN apt install -y unzip wget mysql-server

RUN wget https://github.com/SCADA-LTS/linux-installer/releases/download/v1.5.0/Scada-LTS_v2.7.8.1_Installer_v1.5.0_Setup.zip
RUN unzip Scada-LTS_v2.7.8.1_Installer_v1.5.0_Setup.zip