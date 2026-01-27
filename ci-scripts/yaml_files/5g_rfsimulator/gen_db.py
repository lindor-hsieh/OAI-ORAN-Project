import os

# 定義 SQL 內容 (針對 5G AMF/SMF 格式)
content = """-- MySQL dump for OAI 5G Core
SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";

CREATE DATABASE IF NOT EXISTS `oai_db`;
USE `oai_db`;

-- 1. 建立 5G 認證表 (AMF 用)
DROP TABLE IF EXISTS `AuthenticationSubscription`;
CREATE TABLE `AuthenticationSubscription` (
  `ueid` varchar(15) NOT NULL,
  `authenticationMethod` varchar(10) NOT NULL DEFAULT '5G_AKA',
  `encPermanentKey` varchar(32) NOT NULL DEFAULT 'fec86ba6eb707ed08905757b1bb44b8f',
  `protectionParameterId` varchar(32) NOT NULL DEFAULT 'fec86ba6eb707ed08905757b1bb44b8f',
  `sequenceNumber` varchar(32) DEFAULT '000000000001',
  `authenticationManagementField` varchar(4) DEFAULT '8000',
  `algorithmId` varchar(4) DEFAULT '0000',
  `encOpcKey` varchar(32) DEFAULT 'C42449363BBAD02B66D16BC975D77CC1',
  `encTopcKey` varchar(32) DEFAULT NULL,
  `vectorGenerationInHss` tinyint(1) DEFAULT NULL,
  `n5gcAuthMethod` varchar(32) DEFAULT NULL,
  `rgAuthenticationInd` tinyint(1) DEFAULT NULL,
  `authType` varchar(32) DEFAULT NULL,
  PRIMARY KEY (`ueid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- 2. 建立 5G Session 表 (SMF 用)
DROP TABLE IF EXISTS `SessionManagementSubscriptionData`;
CREATE TABLE `SessionManagementSubscriptionData` (
  `ueid` varchar(15) NOT NULL,
  `servingPlmnid` varchar(15) NOT NULL,
  `singleNssai` varchar(10) NOT NULL,
  `dnnConfigurations` varchar(2000) DEFAULT NULL,
  PRIMARY KEY (`ueid`,`servingPlmnid`,`singleNssai`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;

-- 開始寫入資料
"""

# 生成範圍：1100 ~ 1120 (涵蓋所有 IAB Node 和 UE)
base_imsi = 208990100000000
print(f"Generating IMSI from {base_imsi + 1100} to {base_imsi + 1120}...")

for i in range(1100, 1121):
    imsi = str(base_imsi + i)
    # 寫入認證表
    content += f"INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('{imsi}');\n"
    # 寫入 Session 表
    dnn_config = '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}'
    content += f"INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('{imsi}', '20899', '{{\"sst\": 1, \"sd\": \"16777215\"}}', '{dnn_config}');\n"

content += "COMMIT;\n"

# 寫入檔案 oai_db.sql
with open("oai_db.sql", "w") as f:
    f.write(content)

print("Success! oai_db.sql has been updated.")