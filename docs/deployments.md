# BALIO deployment catalogue

Research date: **20 October 2026**. BALIO means **Balanced Logical and physical I/O for Oracle**. It is an independent benchmark, not an Oracle product or a certification from Oracle or any cloud provider.

`--platform` identifies the database's deployment for the report. All platforms use the same Oracle Net workload path. The flag does not provision cloud infrastructure, change provider limits, or prove support for a database release. Schema deployment and seeding happen through the existing database connection.

This catalogue covers the major Oracle-managed services, hyperscaler offerings, self-managed IaaS, engineered systems, and on-premises infrastructure. An exhaustive list of every hosting company and private installation is not possible; `--platform other` accepts that open-ended set without inventing a vendor certification. Availability means the provider documents the deployment, not that BALIO has been tested there. **No live database was connected during implementation.**

## Canonical platform values

The machine-readable catalogue is [`platforms.json`](../balio/platforms.json). It includes provider-neutral `family` values, aliases, concise caveats, and sources. A service family permits useful comparisons across clouds without treating the cloud name as a performance characteristic.

### OCI and customer-location services

| `--platform` | Deployment | Principal distinction |
|---|---|---|
| `oci-base` | Base Database Service | General-purpose DB systems; shape and edition matter. |
| `oci-exadata-dedicated` | Exadata Database Service on Dedicated Infrastructure | Customer-dedicated Exadata. Aliases: `exacs`, `exadb-d`. |
| `oci-exadata-exascale` | Exadata Database Service on Exascale Infrastructure | Shared Exascale service. Alias: `exadb-xs`. |
| `oci-autonomous-serverless` | Autonomous AI Database Serverless | Managed service with resource controls and autoscaling. |
| `oci-autonomous-dedicated` | Autonomous AI Database on Dedicated Exadata Infrastructure | Autonomous management on dedicated infrastructure. |
| `oci-compute` | Self-managed database on OCI VM or bare metal | Customer controls database and host software. |
| `exadata-cloud-at-customer` | Exadata Database Service on Cloud@Customer | Oracle-managed Exadata at customer location. Alias: `exacc`. |
| `autonomous-cloud-at-customer` | Autonomous AI Database on Exadata Cloud@Customer | Autonomous database at customer location. |
| `compute-cloud-at-customer` | Self-managed database on Compute Cloud@Customer | An independently installed database on local OCI Compute infrastructure. |

OCI's service documentation distinguishes [Base Database](https://docs.oracle.com/en/cloud/paas/base-database/about/), [Exadata deployment models](https://www.oracle.com/engineered-systems/exadata/database-service/faq/), [Autonomous Serverless](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/autonomous-provision.html), and [Autonomous Dedicated](https://docs.oracle.com/en/cloud/paas/autonomous-database/dedicated/adbaa/oracle-database-features-in-autonomous-ai-database-on.html). OCI Compute is also an independently managed database deployment option in Oracle's [deployment overview](https://blogs.oracle.com/database/oracle-ai-database-private-agent-factory-enterprise-faq). Compute Cloud@Customer supplies compute, storage and networking; using it does not imply a Base Database Service installation. See [Compute Cloud@Customer](https://www.oracle.com/cloud/compute/cloud-at-customer/).

Dedicated Region, Alloy, EU/UK sovereign, government and isolated regions describe **where and under whose control a service runs**. Retain the applicable OCI service key and record the region, realm, operator, dedicated/sovereign arrangement and connectivity alongside the result. Availability differs by realm. Oracle describes these choices in its [distributed cloud catalogue](https://www.oracle.com/cloud/distributed-cloud/solutions/) and [regional service availability](https://www.oracle.com/cloud/distributed-cloud/service-availability/). A disconnected location requires a Linux driver inside its reachable network.

### Oracle database services inside partner clouds

| `--platform` | Oracle service | Host cloud |
|---|---|---|
| `oracle-aws-exadata` | Exadata Dedicated | AWS |
| `oracle-aws-exascale` | Exadata Exascale | AWS |
| `oracle-aws-autonomous-dedicated` | Autonomous Dedicated | AWS |
| `oracle-aws-autonomous-serverless` | Autonomous Serverless | AWS |
| `oracle-azure-base` | Base Database | Azure |
| `oracle-azure-exadata` | Exadata Dedicated | Azure |
| `oracle-azure-exascale` | Exadata Exascale | Azure |
| `oracle-azure-autonomous-dedicated` | Autonomous Dedicated | Azure |
| `oracle-azure-autonomous-serverless` | Autonomous Serverless | Azure |
| `oracle-gcp-base` | Base Database | Google Cloud |
| `oracle-gcp-exadata` | Exadata Dedicated | Google Cloud |
| `oracle-gcp-exascale` | Exadata Exascale | Google Cloud |
| `oracle-gcp-autonomous-serverless` | Autonomous Serverless | Google Cloud |

These are Oracle-operated database services deployed inside partner-cloud data centres. They are different from installing Oracle on an EC2, Azure or Compute Engine VM. Current sources list the above families for [Oracle AI Database@AWS](https://docs.oracle.com/en-us/iaas/Content/database-at-aws/oaaws.htm), [Oracle AI Database@Azure](https://www.oracle.com/cloud/azure/oracle-database-at-azure/faq/), and [Oracle Database@Google Cloud](https://docs.cloud.google.com/oracle/database/docs/overview). The current Google service catalogue lists Serverless Autonomous; this catalogue does not infer a Google Autonomous Dedicated offering merely because another partner offers it.

AWS Exascale became generally available in August 2026, so older two-service AWS comparisons are incomplete. See the [Oracle release history](https://docs.oracle.com/en-us/iaas/Content/database-at-aws/oaaws-whats-new.htm). AWS's current [region table](https://docs.aws.amazon.com/odb/latest/UserGuide/getting-started.html) lists Autonomous Serverless only in Northern Virginia and Oregon; the broader service footprint must not be applied to every service. Azure's [service-by-region matrix](https://learn.microsoft.com/en-us/azure/oracle/oracle-db/oracle-database-regions) and Google's [regions and zones](https://docs.cloud.google.com/oracle/database/docs/regions-and-zones) also distinguish individual services. Confirm a particular region, availability zone, shape and tenancy capacity before planning a comparison; roadmap regions are not deployable capacity.

An application in one cloud can alternatively connect to an Oracle database in OCI or another location over interconnect, private connectivity or VPN. Classify the **database's actual host**, and record both client and database locations. Oracle's [Azure FAQ](https://www.oracle.com/cloud/azure/oracle-database-at-azure/faq/) distinguishes the partner-hosted database service from the standalone OCI-Azure Interconnect. A cross-cloud client measures network delay as part of the workload path.

### Other public-cloud and hosted infrastructure

| `--platform` | Deployment | Scope |
|---|---|---|
| `aws-rds` | Amazon RDS for Oracle | AWS-managed Oracle service with restricted administration. |
| `aws-rds-custom` | Amazon RDS Custom for Oracle | Custom managed service; retirement announced. |
| `aws-ec2` | Oracle on Amazon EC2 | Self-managed VM or supported dedicated configuration. |
| `azure-vm` | Oracle on Azure VMs | Self-managed or partner-managed database. |
| `gcp-compute` | Oracle on Google Cloud Compute | Self-managed Oracle on eligible VM configurations. |
| `gcp-bare-metal` | Google Cloud Bare Metal Solution | Dedicated hardware; customer-managed database. |
| `ibm-power` | IBM Power Virtual Server | Oracle on AIX/PowerVM. |
| `ibm-bare-metal` | IBM Cloud Bare Metal | Self-managed Oracle on dedicated hardware. |
| `alibaba-ecs` | Alibaba Cloud ECS | Independently installed Oracle database. |
| `other` | Other IaaS, hosting or private deployment | Describe the provider, topology and exact release. |

AWS separately documents [RDS for Oracle](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Oracle.html), [RDS Custom for Oracle](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/working-with-custom-oracle.html), and [Oracle on EC2](https://docs.aws.amazon.com/en_us/prescriptive-guidance/latest/patterns/migrate-an-on-premises-oracle-database-to-oracle-on-amazon-ec2.html). **RDS Custom for Oracle reaches end of support on 31 March 2027** according to AWS's current notice. Include it for existing estate comparisons; check that notice before selecting it for new work.

Microsoft documents [Oracle on Azure VMs](https://learn.microsoft.com/en-us/azure/oracle/oracle-azure-overview). Google documents [Oracle on Compute](https://docs.cloud.google.com/oracle/compute/plan-for-oracle-on-compute) and [Bare Metal Solution](https://cloud.google.com/bare-metal). IBM documents [Oracle on Power Virtual Server](https://cloud.ibm.com/docs/power-iaas?topic=power-iaas-use-case-oracle) and describes a deployed [Oracle bare-metal example](https://www.ibm.com/case-studies/geva-group). Alibaba's [Oracle backup documentation](https://www.alibabacloud.com/help/en/cloud-backup/user-guide/back-up-an-oracle-database) confirms independently installed Oracle databases on ECS; that evidence does not turn Alibaba RDS into an Oracle managed service.

Provider documentation is evidence of a deployment route, not a universal Oracle certification or licensing entitlement. Check the precise database/OS/hypervisor/CPU combination in the applicable vendor support matrix. The remote Linux driver does not require the database server itself to run Linux; AIX, Windows or another supported database-server OS can expose the same Oracle Net protocol.

### On premises and private infrastructure

| `--platform` | Deployment |
|---|---|
| `onprem-exadata` | Customer-operated Exadata Database Machine. |
| `onprem-exascale` | Customer-operated Exadata using Exascale storage. |
| `onprem-oda` | Oracle Database Appliance. |
| `onprem-server` | Physical servers or virtual machines with supported Oracle installations. |
| `other` | Container/Kubernetes, VMware hosted clouds, private hosted services, specialised or new topology. |

Oracle documents 26ai on [Exadata, ODA and x86-64 Linux](https://docs.oracle.com/en/database/oracle/oracle-database/index.html), while exact ODA combinations belong to the [appliance release documentation](https://www.oracle.com/goto/oda/docs). Existing older releases require their own supported hardware and OS combinations. Exascale is a storage architecture within the Exadata ecosystem, not a standalone replacement SAN for arbitrary Oracle servers. It is available for on-premises Exadata and also underlies a distinct shared cloud service; see [Oracle's Exascale deployment explanation](https://blogs.oracle.com/database/exascale-oracle-database-storage-in-the-age-of-the-cloud).

Single-instance, RAC, Data Guard, sharding/Globally Distributed Database, containerisation and virtualisation describe topology. They should not be confused with hosting providers. Record them separately. BALIO targets one configured service endpoint per invocation; benchmarking every shard, role or placement requires explicit endpoint-specific runs and a test plan. A read/write workload needs a writable service; a physical standby opened read-only is not interchangeable.

## Version and infrastructure interpretation

**BALIO's intended SQL/client compatibility range of Oracle 12c through 26ai is separate from a service's current provisioning range.** Do not conclude that a service supports 12c because the benchmark uses 12c-compatible SQL. End-to-end certification requires executing deployment, seed, read, write, telemetry and cleanup checks against each exact target release and service.

| Service | Current documented release guidance | Qualification |
|---|---|---|
| OCI Base Database | 19c and 26ai; detailed docs also include 21c on some shapes. | Current marketing FAQ and detailed shape documentation differ; use the selected shape's available image list. |
| Exadata services | Current Oracle FAQ lists 19c and 26ai. | Historical releases and migration paths are not a promise of new provisioning. |
| Autonomous Serverless/Dedicated | 19c and 26ai are documented choices. | Region, parent container version and service lifecycle govern availability. |
| Oracle AI Database@AWS | Current service matrix lists 19c and 26ai. | Check service and region separately. |
| Amazon RDS for Oracle | 19c, 21c and 26ai. | AWS explicitly lists 12c as no longer supported. |
| Google Cloud Compute Oracle planning | The guide currently lists 19c and 23ai. | Confirm release terminology, available binaries and support before assuming 26ai. |
| Self-managed/on-premises | Determined by installed release and platform. | BALIO compatibility is not vendor support or a certification. |

Sources: [Base FAQ](https://www.oracle.com/database/base-database-service/faq/) and [shape-specific documentation](https://docs.oracle.com/en/cloud/paas/base-database/about-dbs/); [Exadata FAQ](https://www.oracle.com/engineered-systems/exadata/database-service/faq/); [Autonomous Serverless provisioning](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/autonomous-provision.html) and [Dedicated versions](https://docs.oracle.com/en/cloud/paas/autonomous-database/dedicated/adbaa/oracle-database-features-in-autonomous-ai-database-on.html); [AWS service matrix](https://docs.oracle.com/en-us/iaas/Content/database-at-aws/oaaws.htm); [RDS engine support](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Oracle.html); [Google planning guide](https://docs.cloud.google.com/oracle/compute/plan-for-oracle-on-compute).

Oracle 26ai release naming can coexist with 23-series version identifiers: Oracle explicitly documents Autonomous API responses that retain `23ai` while the console displays `26ai`. Preserve the full database version and banner in evidence instead of deriving support from one major integer. See [Oracle's rebranding release note](https://docs.oracle.com/en-us/iaas/releasenotes/autonomous-database-dedicated/adbd-service-rebranding.htm).

Exascale storage also has separate software prerequisites. The shared ExaDB-XS service supports 19c, while Exascale on a particular dedicated or customer-owned deployment may impose newer database/Grid Infrastructure minimums. Do not transfer one service's version list to another storage configuration. Compare [ExaDB-XS release notes](https://docs.oracle.com/en/engineered-systems/exadata-database-exascale/exdxs/exa-whats-new.html) with [Exadata Dedicated release notes](https://docs.oracle.com/en/engineered-systems/exadata-cloud-service/ecscm/exa-whats-new.html).

## What a comparable run must record

Record the canonical platform key plus database version/RU, edition, hardware or shape, allocated CPU, memory, storage capacity/tier and limits, RAC instance/service placement, client host location and CPU, network path, encryption, compression, autoscaling, resource-manager service and concurrent workloads. Record schema/table sizes, actual allocated bytes and block size. If a field is inaccessible on a managed service, say so.

Logical-read-heavy and physical-read-heavy tests can run through any reachable Oracle Net endpoint with the needed schema privileges. Their achieved ratio depends on cache residency and the service's policies. A database physical read can be served by Exadata flash or another storage cache; it is not proof of a read from underlying flash media or disk. Use database counters and, where authorised and available, provider/storage telemetry to state exactly what was measured. See the benchmark's measurement documentation for definitions and limitations.

For Autonomous and restricted managed services, available privileges, service consumer groups, session limits and quotas constrain both the run and observability. A successful workload without database counter access proves client operations and latency, not measured physical IOPS. Retain unavailable metrics as unavailable. Do not claim a raw infrastructure maximum from a quota-limited database or a network-bound client.
