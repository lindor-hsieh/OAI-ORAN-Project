-- MySQL dump for OAI 5G Core
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
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001100');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001100', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001101');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001101', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001102');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001102', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001103');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001103', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001104');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001104', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001105');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001105', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001106');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001106', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001107');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001107', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001108');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001108', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001109');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001109', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001110');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001110', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001111');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001111', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001112');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001112', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001113');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001113', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001114');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001114', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001115');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001115', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001116');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001116', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001117');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001117', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001118');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001118', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001119');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001119', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
INSERT INTO `AuthenticationSubscription` (`ueid`) VALUES ('208990100001120');
INSERT INTO `SessionManagementSubscriptionData` (`ueid`, `servingPlmnid`, `singleNssai`, `dnnConfigurations`) VALUES ('208990100001120', '20899', '{"sst": 1, "sd": "16777215"}', '{"oai":{"pduSessionTypes":{"defaultSessionType":"IPV4"},"sscModes":{"defaultSscMode":"SSC_MODE_1"},"5gQosProfile":{"5qi":9,"arp":{"priorityLevel":8,"preemptCap":"NOT_PREEMPT","preemptVuln":"NOT_PREEMPTABLE"},"priorityLevel":8},"sessionAmbr":{"uplink":"1000 Mbps","downlink":"1000 Mbps"}}}');
COMMIT;
