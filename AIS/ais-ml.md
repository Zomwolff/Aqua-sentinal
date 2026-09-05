To confirm the Machine Learning model is working and to evaluate its accuracy, you can use the following methods. Since it runs completely in the background, you have to look at the logs and the database to see it in action.

1. How to confirm it is training on its own
The Isolation Forest model runs on an automatic 1-hour loop inside the anomaly-detection container.

Check the Logs: You can run this command in your terminal to see the exact moments the AI wakes up and trains itself:

bash


docker logs aqua-sentinal-anomaly-detection-1 2>&1 | grep "Isolation Forest"
Once it gathers enough data (100 feature windows), you will see a log that looks like this: [INFO] app.ml_model: Successfully trained and saved Isolation Forest on 142 samples.

Check the Database: Once the model is trained, it will start predicting anomalies on live data. You can confirm it is working by checking DBeaver (in the anomaly_events table). Look for any rows where the source column says isolation_forest. If you see rows with that source, the ML model is fully alive and catching things!

2. How to check if it's "Accurate"
Because Isolation Forest is an unsupervised AI model, it doesn't have a simple "95% accuracy" score (because we don't have a dataset of known oil spills to grade it against). Instead, it operates by learning what "normal" looks like, and flagging the top 1% of weirdest behavior.

Here is how you evaluate its real-world accuracy:

A. The "Sanity Check" (Cross-referencing) When you see an anomaly from isolation_forest in the anomaly_events table, grab the mmsi (Ship ID) and window_start time. Then, go to your vessel_features table and look at what that ship was doing at that exact time.

If you see the ship had a crazy speed_variance or an erratic max_rate_of_turn, the AI is highly accurate—it successfully caught bizarre behavior.
If the numbers look completely normal, the AI is throwing a "false positive" and we need to tweak it.
B. The "Gap Analysis" The biggest test of accuracy is seeing what the AI catches that the static rules missed. If a ship is performing a highly suspicious, complex maneuver (like slowly circling while varying its speed), the static rules might not catch it because it didn't break a specific hard-coded limit. The ML model, however, looks at all the features at once. If the ML model flags a ship that the static rules ignored, it proves the AI is working as intended!