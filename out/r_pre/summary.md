# Pre-event placebo (event 2022-07-09, matching on June 9-30 only)

No data after 2022-08-07 read. Caveat: transfer count, ether balance and wallet age come from g4 and cover
the full 60-day baseline, not June only; holdings and USDC share are recomputed from June panel days.
Issuer mint/redeem control screen: OFF (2 controls flagged).

```
    usdc_mode    cap n_exposed dropped_exposed n_controls n_strata     beta_D
       <char> <lgcl>     <int>           <int>      <int>    <int>      <num>
1: three_bins  FALSE      3785              18       9548      667 -0.9362424
2: three_bins   TRUE      3785              18       9548      667  0.0009391
3:       none  FALSE      3803               0      10064      263  0.1331098
4:       none   TRUE      3803               0      10064      263 -0.0218930
5: continuous  FALSE      3790              13       9552      657  0.0350778
6: continuous   TRUE      3790              13       9552      657  0.0170951
   se_D_wallet       lo      hi se_D_stratum case_D  beta_A  se_A case_A
         <num>    <num>   <num>        <num>  <int>   <num> <num>  <int>
1:     1.17317 -3.23582 1.36333      1.16811      2 -1.3145 1.272      2
2:     0.01916 -0.03662 0.03850      0.01915      3 -1.3145 1.272      2
3:     0.86846 -1.56919 1.83541      0.87435      2 -0.5590 1.052      2
4:     0.01786 -0.05689 0.01311      0.01865      3 -0.5590 1.052      2
5:     0.86162 -1.65382 1.72398      0.86256      2 -0.5231 1.042      2
6:     0.01927 -0.02067 0.05486      0.01992      3 -0.5231 1.042      2
   power_m015 se_wallet_level power_m015_projected
        <num>           <num>                <num>
1:    0.05188         1.17308              0.05281
2:    1.00000         0.01916              1.00000
3:    0.05342         0.86840              0.05514
4:    1.00000         0.01785              1.00000
5:    0.05348         0.86156              0.05522
6:    1.00000         0.01926              1.00000
```
