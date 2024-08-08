This repo contains many tutorials for anomaly detection, mostly focused on the weakly supervised techniques and in particular the CATHODE method. 
For our purposes, we will stick to very basic tutorials based on simple Gaussian data that will illustrate the basic concepts.
All should work with the NERSC `pythorch 2.0.1` kernel (some with additional `pip` installs)
These two basic tutorials are `demos/autoencoder_gauss.ipynb` `demos/weak_supervision_gauss_example.ipynb`.

For each tutorial after the basic walkthrough is done we encourage students to explore these concepts further and play around with various choices to get a
better intuition for these methods. 

Once these basic tutorials are completed, they can explore the additional tutorials in `demos` which feature realistic physics data (specifically focused on jet substructure anomalies).
The `weak_supervision.ipynb` tutorial is similar to the Gaussian example but on realistic physics data.
The `cathode_walkthrough.ipynb` includes the next step of a realistic usage where one trains a generative model to learn the background distribution to be used in weak supervision. 
