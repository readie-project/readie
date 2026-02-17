# checkpoint-restore-for-serverless

<img width="5110" height="1966" alt="image" src="https://github.com/user-attachments/assets/fd7994c9-ac79-4e59-b2e5-cd345f868e61" />

## Main/Router node
The main node accepts all incoming execution requests, and executes them through a series of processes to allocate a worker with the execution task. The main node is connected to multiple worker nodes which are provisioned based on the load and compute requirements. The series of processes are explained below:

### Code Analysis
The user's code is parsed to extract resources (packages, models, datasets) required to execute the code. This is done by parsing the AST of the code. Input variables/arguments are identified and profiled to get their type, size and other metadata.

A Merkle tree is created with a TTL to track changes in its arguments. In the case the same function is being requested for execution with different arguments, a traversal of the Merkle tree allows to only transfer those arguments whose values have changed since the last execution.

### Resource Estimation
We have a model to predict how much compute resources (CPU, GPU) will a function require given the metadata of the input variables (as obtained in the previous step). The source of the prediction model can be accessed [here](https://github.com/illinoisdata/python-execution-memory-prediction). This step tries to minimize over-provisioning of resources over static minimum provisioning.

### State Table
The state table is connected to the workers via a gRPC Client Streaming endpoint. The workers send over utilization and the current processes, container states and configurations over to the main node, which are stored in the state table. The state table keeps track of all active workers, and ongoing executions and idle containers inside each of them and also active requests.

### Router
Based on the data collected from the code analysis, resource estimation and the state table values, the route creates the required configuration and makes the decision to direct the request to a particular worker. After shortlisting the workers which have the available compute to run the function based on the resource estimation, the router has to choose the most suitable idle container given the configuration or choose from a set of checkpoints, the best suited to run the given configuration - minimizing both startup times and idle resource times. Below are functions based on which decisions are taken by the router.

#### Cold Start Time
This is the time taken to ready a container when a particular checkpoint is restored. Given the resources required in the code, the checkpoint image which minimizes the startup time is selected. It is calculated using the below equation:

(equation to be added later)

#### Maximizing Warm Container Resuability

##### For repeating requests/requests from the same session
Containers are kept warm for a specified time (TTL), before they are destroyed. If a request is received from the same session or same function while the container is alive, it is directly assigned to that container and only the changed arguments are streamed to the worker.

##### For new requests
In order to reuse warm containers without compromising on startup times, we compare the existing idle container configurations to serve the request by calculating partial initialization times using the below equation. We then compare the calculated cold start time from above to the obtained warm start times, and select one of the idle containers if the time delta is within a reasonably small threashold.

(equation to be added later)

#### Maximizing Checkpoint Density
Grouping together executions which share the same base checkpoint, allows them to share the initialized resources (packages, models, datasets) on restore, until any of the containers write to the resources in which case a copy of the resource is made into its memory segment i.e. Copy-on-Write (CoW). We want to increase the number of containers running the same base checkpoint inside the same worker (i.e. checkpoint density) as this allows to share the memory allocated to the resources amongst all the containers using them. At the same time, it also allows faster fetching of the checkpoint, as it would be present in the local SSD of that worker already, instead of pulling it from the remote storage.

## Worker nodes
Each worker node is a set of GVisor containers either idling or running different function executions. These containers are managed by the proxy service running inside each worker. The worker is responsible to execute the functions and stream back the output to the main node, which then streams it back to the user as the response.

Every worker node has a local NVMe SSD attached to it which loads frequently used resources and recently used checkpoints on initialization for faster access. If a resource or checkpoint requested does not exist in the local SSD it is then pulled from the remote storage.

### Proxy Service
The proxy service acts as the orchestrator of the containers inside the worker, written in GoLang for better concurrency control. It uses the Docker GoLang SDK to provision, destory, pause, resume and restore containers. It also monitors each container's allocated compute for memory overflow. The proxy service has an active connection to the main node always, and sends utilization and allocation information against each container to the main node via gRPC.

#### Queuing
When a new request arrives with a configuration, it is first queued with the proxy service until a container with the required configuration inside the worker becomes available or a new container is provisioned.

#### State table
Contains the same data as the state table of the main node, scoped only for the containers present inside the worker.

### Unix Domain Socket
The proxy service establishes a bi-directional connection to each of the containers using a Unix Domain Socket (UDS). This is used to send new execution requests to the provisioned containers and receive execution statuses, outputs and errors from them.

## Advantages of this architecture over standard FaaS
The comparisions are drawn by comparing the features and optimizations with AWS Lambda (generic), GCP Cloud Functions (generic), Modal AI (Python), Beam Cloud (Python) RunPod (Python).

### Efficient Storage
Using checkpointing features such as AWS SnapStart and Modal's checkpoint-restore for functions, requires a checkpoint to be stored for each serverless function. This leads to high storage costs which are then transferred to users in exchange for lower cold start times. The above strategy focuses on having generalized checkpoints which can server multiple serverless execution requests, saving storage and can be a storage cost v/s start time tradeoff.

### Reduced Cold Start Time
Using checkpoint-restore for reducing cold starts only works when the function to execute is already available with the provider and has been executed at least once. However, the startup time is still high for functions which just execute once or are under development. The above strategy makes use of generalized checkpoints to match a request to the checkpoint which can execute it with the least initialization time. These checkpoints are created based on historical requests, aiming to minimize the cold start initialization time across all of them, and to determine sets of resources to do so by solving the below equation:

(equation to be added later)

### Minimal Over-provisioning
In traditional serverless platforms, the user has to define the compute resources required to execute the serverless functions. However, many times the allocated compute is not used to the limit and the compute resources are wasted and at the same time, the user is also charged for them. The resource prediction model developed allows to allocate only the required compute resources and scale-up in case more compute is required. The model adapts with changing function arguments and this reduces the compute that remains idle due to over-provisioning along with the checkpoint density maximization policies of the router.

### Tiered placement of resources and checkpoints
Unlike a static placement of resources across three levels in Modal - high speed cache, local SSD and remote storage, the strategy is to dynamically allocate the checkpoints and resources, which would be different for each worker. This is done based on the functions being executed currently and past executions in the worker, in order to reduce startup times by increasing hits, reducing local SSD storage costs by keeping only those resources which are required and rarely accessing the remote storage. The placements are determined using the below equation:

(equation to be added later)

